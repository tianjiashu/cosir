"""构建面向模型的系统提示词（五层结构）。

系统提示词按层构建，每层一块，边界清晰（拼接顺序即下列顺序）：

1. ``<runtime_context>`` 动态变量层：运行期才确定的事实（身份与角色、操作系统、工作区根目录
   与写入边界、工具集合、用户语言），直接由 ``AgentProfile`` / ``Settings`` / 系统状态注入，
   不读取任何文件。
2. ``<agent_layer>`` Agent 系统预设层：所有 profile 都使用装配阶段载入的
   ``AgentProfile.system_prompt`` 正文。该层是系统预设，与用户无关；本层只负责施加预算，
   不访问提示词文件或做变量替换。
T. ``<tool_layer>`` 工具能力目录层：只在 ``AgentProfile.allowed_tools`` 包含委派工具
   （``delegate_task``）时生成，内容为 ``AgentProfileRegistry.child_agent_summary`` 产出的
   子 Agent 目录。委派工具不可用（子 Agent 已禁用委派、目录为空）时整层不出现，避免向模型下发
   不存在的契约；本层只消费调用方传入的工具名集合，不自行推导工具可用性。
3. ``<global_layer>`` 系统级全局指令层：来源唯一、路径固定为 ``<system_cosir_dir>/AGENTS.md``
   （由 ``app.utils.cosir_paths.system_instruction_file`` 计算），作为跨所有 workspace 生效的
   全局提示词；文件缺失时创建空白文件供用户编辑并降级为空，读取失败（权限/编码/IO）时同样降级为空，
   空白内容不生成该层标签；不参与目录层级择优。
4. ``<workspace_layer>`` Workspace 项目层：只认 ``AGENTS.md``（唯一候选文件名，见
   ``_WORKSPACE_INSTRUCTION_FILE_NAME``），按目录层级择优（顶层优先）选出**唯一**一个项目
   指令文件，受预算闸门约束，避免上下文爆炸。

空层块（``""``）不参与拼接，避免相邻层之间出现多余空行。

各层预算（token 上限与字节安全兜底）的唯一事实源是 ``Constant.SystemPrompt``：token 上限决定
某一层注入多少内容，字节上限是同层的廉价截断（先于 token 估算执行，避免超大文件进入 O(n)
估算）。这些数值都是固定常量、不经环境变量覆盖，故不在 ``Settings``；本模块不再自带任何预算
字面量。
"""

from __future__ import annotations

import logging
import os
from collections import deque
from collections.abc import Collection
from pathlib import Path
from platform import system

from app.config.constant import Constant
from app.config.settings import Settings
from app.core.agents.agent_profile import AgentProfile
from app.core.tools.schemas.tool_names import TOOL_DELEGATE_TASK
from app.utils.cosir_paths import system_instruction_file
from app.utils.file_utils import read_text_file
from app.utils.token_estimator import TokenEstimator

logger = logging.getLogger(__name__)

# 扫描 workspace 指令文件时跳过的目录（与 tools 层 ignore_rules.DEFAULT_IGNORED_DIR_NAMES
# 语义一致；此处局部定义以避免 core/context 反向依赖 tools 层）。如后续下沉到公共位置可统一替换。
_IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "dist",
        "build",
        ".idea",
        ".vscode",
        ".svn",
        ".hg",
    }
)

# Workspace 项目指令文件名（唯一候选）。只有一个候选名，故不存在"文件名优先级"维度，
# 择优只剩目录层级一条；其它常见指令文件（如 CLAUDE.md）一律不参与择优、不会被加载。
_WORKSPACE_INSTRUCTION_FILE_NAME = "AGENTS.md"


class SystemPromptBuilder:
    """按五层结构构建本地 coding-agent 的系统提示词。

    本类为无状态工具类，所有构建逻辑均为静态方法，不持有实例状态。
    """

    @staticmethod
    def build(
            agent_profile: AgentProfile,
            workspace_root: str,
    ) -> str:
        """构建完整系统提示词文本（五层）。

        参数:
            agent_profile: 当前执行主体的 Agent 档案；其 ``allowed_tools`` 决定工具能力目录层
                是否生成（含委派工具才生成）。
            workspace_root: 当前工作区根目录。

        返回:
            由五层层块拼接出的系统提示词（空层块不参与拼接）；Layer 1 动态变量 + Layer 2 系统
            预设 + Layer T 工具能力目录 + Layer G 系统级全局指令 + Layer 3 workspace 项目指令。

        异常:
            RuntimeError: ``allowed_tools`` 含委派工具、但进程级 Agent 目录尚未初始化时，由
                ``configuration.get_agent_registry`` 抛出（装配错误，不静默降级）。

        副作用:
            读取系统级全局指令文件与 workspace 指令文件（失败均容错）；委派工具可用时额外读取
            进程级 Agent 目录；Agent 系统提示词已在 profile 装配阶段载入。
        """
        layer1 = SystemPromptBuilder._build_runtime_context(agent_profile, workspace_root)
        layer2 = SystemPromptBuilder._build_agent_layer(agent_profile)
        tool_layer = SystemPromptBuilder._build_tool_layer(
            workspace_root, agent_profile.allowed_tools
        )
        global_layer = SystemPromptBuilder._build_global_layer()
        layer3 = SystemPromptBuilder._build_workspace_layer(workspace_root)
        blocks = [layer1, layer2, tool_layer, global_layer, layer3]
        return "\n\n".join(block for block in blocks if block)

    # --- Layer 1: 动态变量层（运行期事实，不读文件） ---
    @staticmethod
    def _build_runtime_context(agent_profile: AgentProfile, workspace_root: str) -> str:
        """构建动态变量层：身份与角色、操作系统、工作区根目录与写入边界、工具集合、用户语言。

        文案为英文（读者是模型，与工具描述、子 Agent 选择指南保持同一语言）。本层只承载
        当前实现确认的运行期事实。

        参数:
            agent_profile: 当前执行主体的 Agent 档案。
            workspace_root: 当前工作区根目录。

        返回:
            包裹在 ``<runtime_context>`` 标签内的动态变量文本。

        异常:
            无。

        副作用:
            读取 ``Settings``（用户语言）、``Constant.SystemPrompt``（字节上限）与
            ``platform.system()``；不读任何用户/workspace 文件。
        """
        language = Settings.DEFAULT_LANGUAGE
        allowed = ", ".join(agent_profile.allowed_tools) or "none"
        lines = [
            "<runtime_context>",
            (
                "You are cosir, an agent running on the user's local machine "
                f"(role: {agent_profile.role}). Your input consists of the user's messages, "
                "your own earlier messages, and system notices."
            ),
            f"- OS: {system()}",
            f"- Workspace root: {workspace_root}",
            (
                "- Workspace rules: all code lives under the workspace root. File edits and "
                "deletions are allowed only inside it; anything outside is rejected by the "
                "system. .cosir/ is a reserved read-only area holding runtime metadata: you "
                "may read it, but writing, editing, deleting, or moving anything inside it "
                "is rejected."
            ),
            f"- Tools available: {allowed}",
            (
                f"- User language: {language}. Reply in that language in a friendly tone; do "
                "not use emoji unless the user asks."
            ),
            "</runtime_context>",
        ]
        text = "\n".join(lines)
        return SystemPromptBuilder._enforce_bytes(
            text, Constant.SystemPrompt.RUNTIME_CONTEXT_MAX_BYTES
        )

    # --- Layer 2: Agent 系统预设层（profile.system_prompt） ---
    @staticmethod
    def _build_agent_layer(agent_profile: AgentProfile) -> str:
        """构建 Agent 系统预设层并施加预算上限。

        参数:
            agent_profile: 当前执行主体的 Agent 档案（取其 ``system_prompt``）。

        返回:
            包裹在 ``<agent_layer>`` 标签内的系统预设文本。

        异常:
            无。

        副作用:
            提示词超预算时记录 Agent ID，不记录提示词正文。
        """
        raw = agent_profile.system_prompt
        if raw is None or raw.strip() == "":
            return ""
        content = SystemPromptBuilder._enforce_budget(
            raw,
            Constant.SystemPrompt.AGENT_PERSONA_MAX_BYTES,
            Constant.SystemPrompt.AGENT_PERSONA_MAX_TOKENS,
        )
        if content != raw:
            logger.warning(
                "agent_system_prompt_truncated",
                extra={"data": {"agent_id": agent_profile.agent_id}},
            )
        return "<agent_layer>\n" + content + "\n</agent_layer>"

    # --- Layer T: 工具能力目录层（可用工具含委派工具时的子 Agent 目录） ---
    @staticmethod
    def _build_tool_layer(
            workspace_root: str,
            available_tool_names: Collection[str],
    ) -> str:
        """构建工具能力目录层：可用工具含委派工具时投影子 Agent 目录。

        判定与取材都只用两份外部事实：``available_tool_names``（调用方传入的该 Agent 可用工具名
        集合，当前为 ``AgentProfile.allowed_tools``）与进程级 ``AgentProfileRegistry``。委派工具
        不在集合里、或该 workspace 可见的 CHILD 目录为空时返回 ``""``（整层不出现）——提示词不得
        声明不可用的委派能力。

        参数:
            workspace_root: 当前工作区根目录，用作 Agent 目录的作用域键。
            available_tool_names: 该 Agent 声明的可用工具名集合。

        返回:
            包裹在 ``<tool_layer>`` 标签内的子 Agent 目录；不需要该层时返回 ``""``。超字节兜底时
            截断的是目录正文而不是标签，返回文本仍是一对完整标签。

        异常:
            RuntimeError: 委派工具可用但进程级 Agent 目录未初始化（``configuration
                .get_agent_registry`` 的装配错误）时原样抛出，不静默降级。

        副作用:
            读取进程级 Agent 目录的内存索引（不读文件、不修改注册表）；超字节兜底时记录警告。
        """
        if TOOL_DELEGATE_TASK not in available_tool_names:
            return ""
        # 函数内延迟导入：``app.config.configuration`` 模块级导入 ``app.core.tools``，而工具装配
        # 链会反向导入 ``app.core.context`` / ``app.assistant_transport.event``，顶层导入会形成环
        # （详见 ``app/core/tools/__init__.py`` 的 PEP 562 惰性导出说明）。
        from app.config.configuration import get_agent_registry

        summary = get_agent_registry().child_agent_summary(workspace_root).strip()
        if not summary:
            return ""
        layer = "<tool_layer>\n" + summary + "\n</tool_layer>"
        max_bytes = Constant.SystemPrompt.TOOL_LAYER_MAX_BYTES
        if len(layer.encode("utf-8")) <= max_bytes:
            return layer
        logger.warning(
            "tool_layer_truncated",
            extra={"data": {"max_bytes": max_bytes}},
        )
        overhead = len(b"<tool_layer>\n\n</tool_layer>")
        summary = SystemPromptBuilder._enforce_bytes(summary, max_bytes - overhead)
        return "<tool_layer>\n" + summary + "\n</tool_layer>"

    # --- Layer G: 系统级全局指令层（system_cosir_dir/AGENTS.md，跨 workspace 生效） ---
    @staticmethod
    def _build_global_layer() -> str:
        """构建系统级全局指令层：加载 ``<system_cosir_dir>/AGENTS.md`` 作为全局提示词。

        与 Workspace 层（按目录层级择优、仅一个文件）不同，本层来源唯一、路径固定
        （由 ``app.utils.cosir_paths.system_instruction_file`` 计算），作为跨所有 workspace
        生效的全局指令。文件缺失时先创建空白文件（便于用户就地编辑），再降级为空字符串；
        读取失败（权限/编码/IO）时同样降级为空字符串，不中断构建。空白内容（文件存在但无
        实质内容）不生成 ``<global_layer>`` 块，避免向模型注入空标签噪声。

        返回:
            包裹在 ``<global_layer>`` 标签内的全局指令文本；无有效内容时返回 ``""``。

        异常:
            无（创建与读取失败均容错，记日志并降级为空字符串）。

        副作用:
            若 ``<system_cosir_dir>/AGENTS.md`` 不存在则创建空白文件（含父目录）；不修改
            已有文件内容。
        """
        path = system_instruction_file()
        if not path.is_file():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")
            except OSError as exc:
                logger.warning(f"global_instruction_create_failed path={path} error={exc}")
                return ""
        try:
            raw = read_text_file(path)
        except (FileNotFoundError, PermissionError, OSError, UnicodeDecodeError) as exc:
            logger.warning(f"global_instruction_read_failed path={path} error={exc}")
            return ""
        if raw is None or raw.strip() == "":
            return ""
        content = SystemPromptBuilder._enforce_budget(
            raw,
            Constant.SystemPrompt.GLOBAL_INSTRUCTION_MAX_FILE_BYTES,
            Constant.SystemPrompt.GLOBAL_INSTRUCTION_MAX_FILE_TOKENS,
        )
        if not content.strip():
            return ""
        return "<global_layer>\n" + content + "\n</global_layer>"

    # --- Layer 3: Workspace 项目指令层（定位唯一文件 + 预算闸门） ---
    @staticmethod
    def _build_workspace_layer(workspace_root: str) -> str:
        """构建 Workspace 项目指令层：定位唯一的 ``AGENTS.md`` 并施加预算闸门。

        候选文件名唯一（``_WORKSPACE_INSTRUCTION_FILE_NAME``，不再经配置注入），择优只剩
        目录层级一条（越浅越优先），故最多加载一个文件；没有任何命中时返回空字符串。

        参数:
            workspace_root: 当前工作区根目录。

        返回:
            包裹在 ``<workspace_layer>`` 标签内的单个指令文件文本；无命中文件时返回 ``""``。

        异常:
            无（定位/读取失败均容错，记日志并降级为空字符串）。

        副作用:
            遍历工作区目录，并读取命中的唯一指令文件。
        """
        found = SystemPromptBuilder._find_instruction_file(
            Path(workspace_root)
        )
        if found is None:
            return ""
        rel, abs_path = found
        try:
            raw = read_text_file(abs_path)
        except (FileNotFoundError, PermissionError, OSError, UnicodeDecodeError) as exc:
            logger.warning(f"workspace_instruction_read_failed path={abs_path} error={exc}")
            return ""
        # 单文件预算：token 上限与字节安全兜底都取自 ``Constant.SystemPrompt``；字节上限先于
        # token 估算执行，避免超大文件进入二分查找、也防止上下文被撑爆。
        content = SystemPromptBuilder._enforce_budget(
            raw,
            Constant.SystemPrompt.WORKSPACE_INSTRUCTION_MAX_FILE_BYTES,
            Constant.SystemPrompt.WORKSPACE_INSTRUCTION_MAX_FILE_TOKENS,
        )
        return (
            f"<workspace_layer abs_path={abs_path}>\n"
            f"# ./{rel.as_posix()}\n{content}\n</workspace_layer>"
        )

    @staticmethod
    def _find_instruction_file(root: Path) -> tuple[Path, Path] | None:
        """定位唯一的 Workspace 项目指令文件 ``AGENTS.md``（只定位，不读取内容）。

        候选文件名唯一（``_WORKSPACE_INSTRUCTION_FILE_NAME``），因此不存在"文件名优先级"
        维度，择优键只有一个：**目录所在层级越浅越优先**；同层级多命中时以相对路径字典序
        兜底，保证结果确定。文件名比较不区分大小写。

        采用 BFS 逐层扫描 + 在线维护最优项 + 逐层剪枝：
            - 不收集全部命中、不排序、不读取文件内容；
            - 每处理完一个完整目录层级再判定收工：本层只要命中，更深层不可能更优，立即结束
              （根目录命中时只需扫描根目录一层）；
            - 跳过 ``_IGNORED_DIRS`` 与超过固定最大深度（4 层）的目录，不跟随符号链接；
            - 相对路径由绝对路径做纯字符串换算得到，避免逐候选 ``resolve()`` 的额外系统调用。

        参数:
            root: 工作区根目录（内部解析为绝对路径）。

        返回:
            命中文件的 ``(相对路径, 绝对路径)``；工作区内无任何命中时返回 ``None``。

        异常:
            无（目录不可读、路径异常均静默跳过）。

        副作用:
            无（仅遍历目录，不读取文件内容）。
        """
        target = _WORKSPACE_INSTRUCTION_FILE_NAME.lower()
        try:
            root = root.resolve()
        except OSError:
            return None
        if not root.is_dir():
            return None

        best_key: tuple[int, str] | None = None
        best_rel: Path | None = None
        best_abs: Path | None = None
        queue: deque[tuple[Path, int]] = deque([(root, 0)])

        while queue:
            for _ in range(len(queue)):  # 一次迭代覆盖一个完整目录层级
                directory, depth = queue.popleft()
                try:
                    with os.scandir(directory) as entries:
                        for e in entries:
                            if e.name.lower() == target:
                                try:
                                    rel = Path(e.path).relative_to(root)
                                except ValueError:
                                    continue
                                key = (len(rel.parts), rel.as_posix())
                                if best_key is None or key < best_key:
                                    best_key = key
                                    best_rel = rel
                                    best_abs = Path(e.path)
                            elif (
                                    depth + 1 <= 4
                                    and e.name not in _IGNORED_DIRS
                                    and e.is_dir(follow_symlinks=False)
                            ):
                                queue.append((Path(e.path), depth + 1))
                except (PermissionError, OSError):
                    continue
            if best_key is not None:
                break  # 本层已命中，更深层不可能更优

        if best_rel is None or best_abs is None:
            return None
        return best_rel, best_abs

    # --- 预算工具 ---
    @staticmethod
    def _enforce_bytes(text: str, max_bytes: int) -> str:
        """按 UTF-8 字节上限防御性截断（超出则丢弃尾部，避免越界）。

        参数:
            text: 待截断文本。
            max_bytes: 字节上限。

        返回:
            截断后的文本（不超过 ``max_bytes`` 字节）。

        异常:
            无。

        副作用:
            无。
        """
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        return encoded[:max_bytes].decode("utf-8", "ignore")

    @staticmethod
    def _enforce_budget(text: str, max_bytes: int, max_tokens: int) -> str:
        """对文本施加字节与 token 双重预算上限。

        参数:
            text: 待约束文本。
            max_bytes: 字节上限。
            max_tokens: token 上限（启发式估算）。

        返回:
            约束后的文本。

        异常:
            无。

        副作用:
            无。
        """
        text = SystemPromptBuilder._enforce_bytes(text, max_bytes)
        return SystemPromptBuilder._truncate_tokens(text, max_tokens)

    @staticmethod
    def _truncate_tokens(text: str, max_tokens: int) -> str:
        """按 token 估算上限截断文本（二分查找字符边界，纯启发式）。

        参数:
            text: 待截断文本。
            max_tokens: token 上限。

        返回:
            估算 token 不超过 ``max_tokens`` 的前缀。

        异常:
            无。

        副作用:
            无。
        """
        if TokenEstimator.estimate(text) <= max_tokens:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if TokenEstimator.estimate(text[:mid]) <= max_tokens:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo]
