"""系统提示词的配置来源契约：每类「Agent 能看到的配置」怎样投影成逐项内容。

系统提示词创建时固化（缓存前缀不能变），之后配置再变，Agent 看到的仍是创建那一刻的投影。为了日后
告诉它「哪些变了」，每类来源都产出**逐项投影**：条目键 → 指纹 + 进提示词的文本。提示词由这份投影
渲染，Session 记下各条目的指纹作为基准（seq 0 系统行与之后每条配置变化通知的 metadata），
两者同源、不另写一套读取；配置再变时由来源自己说明净差异（``describe``）。

来源只读配置、不写文件；读取失败按来源自己的降级规则投影成一条说明，不中断构建。
"""

from __future__ import annotations

import difflib
import hashlib
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from openai.types.shared_params import FunctionDefinition

from app.models.message_metadata import PromptSourceEntry
from app.utils.workspace_instruction import find_workspace_instruction_file

if TYPE_CHECKING:
    from app.core.agents.agent_runtime_config import AgentRuntimeConfig


@dataclass(frozen=True)
class SourceContext:
    """一次投影的输入：哪个工作区、Session 固化了哪些工具、当前是哪个 Agent，以及选中的工作区指令文件。

    ``tool_names`` 决定一类来源在这个 Session 里有没有（没有委派工具就没有子 Agent 目录）；
    ``agent`` 只取身份与预设正文。``workspace_instruction`` 是定位好的 ``(相对路径, 绝对路径)``：定位要
    逐层扫目录（浅层没有 AGENTS.md 的大工作区要扫到第 4 层，实测可达数十毫秒），所以由调用方决定多久
    定位一次——建提示词时现定位，对账时每次驱动定位一次（``RuntimeMessageContextManager.begin_run``）。
    文件内容仍每步按签名校验；只有「新增或删掉 AGENTS.md 文件本身」要等到下一次驱动。
    """

    workspace_root: str
    tool_names: frozenset[str]
    agent: AgentRuntimeConfig
    workspace_instruction: tuple[Path, Path] | None
    # Session 能延迟加载的内置工具定义：延迟工具目录（``DeferredToolsSource``）把它们与外部工具并列。
    deferred_tools: tuple[FunctionDefinition, ...] = ()

    @classmethod
    def locate(
        cls,
        workspace_root: str,
        tool_names: frozenset[str],
        agent: AgentRuntimeConfig,
        deferred_tools: tuple[FunctionDefinition, ...] = (),
    ) -> SourceContext:
        """现定位工作区指令文件并构造投影输入。"""

        found = find_workspace_instruction_file(Path(workspace_root))
        return cls(workspace_root, tool_names, agent, found, deferred_tools)


@dataclass(frozen=True)
class SourceItem:
    """一个条目：``fingerprint`` 判断它变没变，``text`` 是它在提示词里的样子。

    指纹不一定由 ``text`` 算出：Skill 目录只展示名称与描述，正文变了也要算变化，指纹取 ``SKILL.md``
    全文的哈希。
    """

    fingerprint: str
    text: str


SourceItems = Mapping[str, SourceItem]


@dataclass(frozen=True)
class ItemDiff:
    """一个来源相对基准的净差异；键按字典序排列，通知文本因此稳定（不打乱缓存前缀）。"""

    added: tuple[str, ...]
    changed: tuple[str, ...]
    removed: tuple[str, ...]

    @classmethod
    def between(cls, previous: Mapping[str, PromptSourceEntry], items: SourceItems) -> ItemDiff:
        return cls(
            tuple(sorted(key for key in items if key not in previous)),
            tuple(
                sorted(
                    key
                    for key, item in items.items()
                    if key in previous and previous[key].fingerprint != item.fingerprint
                )
            ),
            tuple(sorted(key for key in previous if key not in items)),
        )

    @property
    def empty(self) -> bool:
        return not (self.added or self.changed or self.removed)


class PromptSource(ABC):
    """一类配置来源；实现登记在 ``registry.PROMPT_SOURCES``，提示词渲染、基准记录与对账都遍历它。

    子类必须给出 ``key``（基准与缓存里的来源键，改名等于换了一个来源）并实现全部抽象方法；漏实现在
    实例化时就报错，不会等到配置变化那一刻才发现。
    """

    key: ClassVar[str]

    @abstractmethod
    def applies(self, context: SourceContext) -> bool:
        """这个 Session 的提示词里有没有这一类（按固化的工具名与 Agent 判断）。"""

    @abstractmethod
    def project(self, context: SourceContext) -> dict[str, SourceItem]:
        """读当前配置，投影成逐项内容；只读，不写文件。"""

    @abstractmethod
    def render(self, items: SourceItems) -> str:
        """把投影渲染成提示词里这一块的正文（不含层标签）；没有内容时返回 ``""``。"""

    @abstractmethod
    def describe(
        self, diff: ItemDiff, items: SourceItems, previous: Mapping[str, PromptSourceEntry]
    ) -> str:
        """向 Agent 说明这一类配置相对它上次看到的变化（英文，读者是模型）；只在 diff 非空时调用。

        token 尽量少、信息要全：Agent 据此加上它已看到的旧版本，就能得到现在的完整配置，不必回看文件。
        文档只发 diff（见 :func:`text_change`），目录只报增、删、改的条目；``previous`` 是 Agent 上次
        看到的条目（含原文）。
        """


# 目录与变化通知是给模型的背景信息：不加这句，模型常把「现在多了哪些工具、说明改了什么」当成新消息讲给用户。
# 文案与本仓对齐的 Claude Code 系统通知一致。
_AMBIENT = (
    "This is ambient context — do not narrate it to the user unless they ask or it is directly "
    "relevant to their request."
)


def with_ambient(text: str) -> str:
    """在背景信息末尾追加「不要主动讲给用户」一句；空文本原样返回（没有内容就不提醒）。"""

    return f"{text}\n\n{_AMBIENT}" if text else ""


def fingerprint(*parts: str) -> str:
    """条目指纹：内容的 SHA-256 前 16 位，只用于比较是否变化。"""

    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def text_change(old: str, new: str) -> str:
    """一段文档从 ``old`` 变成 ``new`` 的说明：统一 diff（上下文 2 行）；diff 不比全文短时直接给全文。

    diff 按行计算、不含文件头，Agent 结合提示词里的旧版本即可还原新版本。
    """

    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=2))[2:]
    patch = "\n".join(lines)
    if not old or len(patch) >= len(new):
        return "It now reads:\n" + new
    return "Changes (unified diff against the version you saw):\n" + patch
