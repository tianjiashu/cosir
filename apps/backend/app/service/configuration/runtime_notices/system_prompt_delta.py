"""系统提示词变更的运行时通知。

Session 的系统提示词首次装配时固化，配置变更不改写它；已在本进程装配过的 Session 改为收到一条带
unified diff 的 Session 级通知，下一次推进时写入上下文。本模块不读写配置文件，由各文本配置服务在
写入成功后发布。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfileType
from app.core.workflow.message_queue.session_message import NoticeBody
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces
from app.utils.workspace_scope import normalize_scope_path

if TYPE_CHECKING:
    from app.session_runtime.session_runtime_space import SessionRuntimeSpace


class SystemPromptDeltaSource(str, Enum):
    """发生变化的系统提示词层。"""

    GLOBAL_INSTRUCTIONS = "global_instructions"
    MAIN_AGENT_PROMPT = "main_agent_prompt"
    WORKSPACE_INSTRUCTIONS = "workspace_instructions"


@dataclass(frozen=True)
class SystemPromptDelta:
    """一次系统提示词层的变更：只含新增、删除与修改行的 unified diff，不含完整新提示词。

    ``scope`` 是 workspace 指令所在的 workspace 根目录，其他层为 ``None``。
    """

    source: SystemPromptDeltaSource
    diff: str
    scope: str | None = None

    @classmethod
    def between(
        cls,
        *,
        source: SystemPromptDeltaSource,
        previous: str,
        current: str,
        scope: str | None = None,
    ) -> SystemPromptDelta | None:
        """新旧正文之间的增量；正文没有变化时返回 ``None``，避免无意义广播。"""

        if previous == current:
            return None
        diff = "".join(
            difflib.unified_diff(
                previous.splitlines(keepends=True),
                current.splitlines(keepends=True),
                fromfile="previous",
                tofile="current",
                n=0,
            )
        ).strip()
        if not diff:
            return None
        return cls(source=source, diff=diff, scope=scope)

    def to_notice(self) -> NoticeBody:
        """转换为 Session 级系统通知：简短的覆盖规则加 diff。"""

        scope_line = f"\n作用域：{self.scope}" if self.scope else ""
        content = (
            "<system_prompt_update>\n"
            "系统提示词已变更，请立即应用以下增量：\n"
            "- 未出现在 diff 中的规则保持不变；\n"
            "- 被删除的规则不再生效；\n"
            "- 新规则和修改后的规则优先于旧规则。\n"
            f"来源：{self.source.value}{scope_line}\n"
            "<diff>\n"
            f"{self.diff.rstrip()}\n"
            "</diff>\n"
            "</system_prompt_update>"
        )
        return NoticeBody(
            content=content,
            notice_kind="system_prompt_delta",
            metadata={"source": self.source.value, "scope": self.scope},
        )

    def reaches(self, space: SessionRuntimeSpace) -> bool:
        """这个 Session 是否该收到本次变更。

        只读 space 记下的 Agent 与工作区：``space.agent`` 在本进程首次装配 context 时记下并一直保留；
        context manager 是弱引用、Run 结束后可能已被回收，按它判断会让空闲 Session 视 GC 时机漏收通知。
        也不得顺带创建 manager（会读库，新 Session 还会落库系统提示词）。
        """

        agent = space.agent
        if agent is None:
            return False
        if (
            self.source is SystemPromptDeltaSource.MAIN_AGENT_PROMPT
            and agent.agent_type is not AgentProfileType.MAIN
        ):
            return False
        if self.source is SystemPromptDeltaSource.WORKSPACE_INSTRUCTIONS:
            if self.scope is None:
                return False
            workspace_root = normalize_scope_path(space.current_workspace.root_path)
            return workspace_root == normalize_scope_path(self.scope)
        return True

    def broadcast(self) -> int:
        """投给命中的已物化 Session，返回投递数。

        配置已经写入，通知是旁路：投递失败只记日志并返回 0，不让保存请求失败。
        """

        try:
            delivered = session_runtime_spaces.broadcast_notice(self.to_notice(), self.reaches)
        except Exception:
            log.exception(
                "system_prompt_delta_broadcast_failed",
                extra={
                    "msg": "系统提示词已保存，但变更通知投递失败",
                    "data": {"source": self.source.value, "scope": self.scope},
                },
            )
            return 0
        log.info(
            "system_prompt_delta_broadcast",
            extra={
                "msg": "系统提示词变更已广播到进程内 Session runtime space",
                "data": {
                    "source": self.source.value,
                    "scope": self.scope,
                    "diff_length": len(self.diff),
                    "delivered_count": delivered,
                },
            },
        )
        return delivered
