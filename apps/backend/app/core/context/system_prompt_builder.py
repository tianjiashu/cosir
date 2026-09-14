"""构建面向模型的系统提示词（三层结构）。

系统提示词重构为三层，每层一块，边界清晰：

1. ``<runtime_context>`` 动态变量层：运行期才确定的事实（角色、工作区、日期、工具集合、
   语言、CodeGraph 开关等），直接由 ``AgentProfile`` / ``Settings`` / 系统状态注入，不读取任何文件。
2. ``<agent_layer>`` Agent 系统预设层：来源唯一为 ``AgentProfile.prompt_file_path`` 指向的
   md/txt 文件全文（系统预设，与用户无关）；该字段为 ``None`` 或文件读取失败时使用空的
   规则层，加载后不做变量替换。
3. ``<workspace_layer>`` Workspace 项目层：按文件名优先级（``WORKSPACE_INSTRUCTION_FILE_NAMES``
   的配置顺序，如 ``AGENTS.md`` 优先于 ``CLAUDE.md``）与目录层级优先级（顶层优先）选出
   **唯一**一个项目指令文件，受预算闸门约束，避免上下文爆炸。

预算控制仅在 Layer 2（单文件上限）与 Layer 3（单文件字节 / 单文件 token / 总量 token + 窗口
比例）生效；Layer 1 内容小且固定，仅给字节硬上限防御。
"""

from __future__ import annotations

import logging
import os
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from platform import system

from app.config.settings import Settings
from app.core.agents.agent_profile import AgentProfile
from app.utils.file_utils import read_text_file
from app.utils.token_estimator import TokenEstimator

logger = logging.getLogger(__name__)

# 扫描 workspace 指令文件时跳过的目录（与 tools 层 IGNORED_DIRS 语义一致；此处局部定义
# 以避免 core/context 反向依赖 tools 层）。如后续下沉到公共位置可统一替换。
_IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".coding-agent",
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


class SystemPromptBuilder:
    """按三层结构构建本地 coding-agent 的系统提示词。

    本类为无状态工具类，所有构建逻辑均为静态方法，不持有实例状态。
    """

    @staticmethod
    def build(agent_profile: AgentProfile, workspace_root: str) -> str:
        """构建完整系统提示词文本（三层）。

        参数:
            agent_profile: 当前执行主体的 Agent 档案。
            workspace_root: 当前工作区根目录。

        返回:
            由三层层块拼接出的系统提示词；Layer 1 动态变量 + Layer 2 系统预设
            + Layer 3 workspace 项目指令。

        异常:
            无。

        副作用:
            可能读取 ``prompt_file_path`` 与 workspace 指令文件（失败均容错）。
        """
        layer1 = SystemPromptBuilder._build_runtime_context(agent_profile, workspace_root)
        layer2 = SystemPromptBuilder._build_agent_layer(agent_profile)
        layer3 = SystemPromptBuilder._build_workspace_layer(workspace_root)
        return "\n\n".join([layer1, layer2, layer3])

    # --- Layer 1: 动态变量层（运行期事实，不读文件） ---
    @staticmethod
    def _build_runtime_context(agent_profile: AgentProfile, workspace_root: str) -> str:
        """构建动态变量层：角色、工作区、日期、工具集合、语言、CodeGraph 开关等。

        参数:
            agent_profile: 当前执行主体的 Agent 档案。
            workspace_root: 当前工作区根目录。

        返回:
            包裹在 ``<runtime_context>`` 标签内的动态变量文本。

        异常:
            无。

        副作用:
            读取系统日期与 ``Settings``；不读任何用户/workspace 文件。
        """
        language = Settings.DEFAULT_LANGUAGE
        allowed = ", ".join(agent_profile.allowed_tools) or "none"
        lines = [
            "<runtime_context>",
            f"你是一个运行在用户本机的 {agent_profile.role}。",
            f"当前所处于的操作系统: {system()}",
            (
                f"当前工作区根目录: {workspace_root}，你所有的代码都在这个目录下，"
                f"且写、编辑、删除操作将被系统限制在这个目录下，"
                f"超出这个目录范围的操作将被系统拒绝"
            ),
            f"所拥有的工具集合: {allowed}",
            (
                f"你所面向的用户所使用的语言: {language}，"
                f"请使用友好的语言和用户交流。除非用户要求，不要使用emjio表情回复。"
            ),
            "</runtime_context>"
        ]
        text = "\n".join(lines)
        return SystemPromptBuilder._enforce_bytes(text, Settings.RUNTIME_CONTEXT_MAX_BYTES)

    # --- Layer 2: Agent 系统预设层（profile.prompt_file_path 或内置默认） ---
    @staticmethod
    def _build_agent_layer(agent_profile: AgentProfile) -> str:
        """构建 Agent 系统预设层：加载预设文件并施加预算上限。

        参数:
            agent_profile: 当前执行主体的 Agent 档案（取其 ``prompt_file_path``）。

        返回:
            包裹在 ``<agent_layer>`` 标签内的系统预设文本。

        异常:
            无。

        副作用:
            可能读取 ``prompt_file_path``（失败容错为空规则层）。
        """
        raw = SystemPromptBuilder._load_agent_preset(agent_profile)
        if raw is None:
            raw = ""
        content = SystemPromptBuilder._enforce_budget(
            raw, Settings.AGENT_PERSONA_MAX_BYTES, Settings.AGENT_PERSONA_MAX_TOKENS
        )
        return "<agent_layer>\n" + content + "\n</agent_layer>"

    @staticmethod
    def _load_agent_preset(agent_profile: AgentProfile) -> str | None:
        """加载 Agent 系统预设内容。

        ``prompt_file_path`` 非空时读取该文件；路径为空或读取失败（不存在/无权限/编码错误）
        时返回 ``None``，读取失败会记录 warning，不中断构建。

        参数:
            agent_profile: 当前执行主体的 Agent 档案。

        返回:
            预设文件全文；没有可用文件时返回 ``None``。

        异常:
            无（读取异常均内部兜底）。

        副作用:
            读取 ``prompt_file_path`` 指向的文件。
        """
        path = agent_profile.prompt_file_path
        if path:
            try:
                return read_text_file(path)
            except (FileNotFoundError, PermissionError, OSError, UnicodeDecodeError) as exc:
                logger.warning(f"agent_preset_load_failed path={path} error={exc}")
        return None

    # --- Layer 3: Workspace 项目指令层（定位唯一文件 + 预算闸门） ---
    @staticmethod
    def _build_workspace_layer(workspace_root: str) -> str:
        """构建 Workspace 项目指令层：选取优先级最高的唯一指令文件并施加预算闸门。

        优先级的唯一事实源是 ``WORKSPACE_INSTRUCTION_FILE_NAMES`` 的配置顺序（越靠前越优先）
        叠加目录层级（越浅越优先），故最多加载一个文件；没有任何命中时返回空字符串。

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
            Path(workspace_root),
            Settings.WORKSPACE_INSTRUCTION_FILE_NAMES,
            Settings.WORKSPACE_INSTRUCTION_MAX_DEPTH,
        )
        if found is None:
            return ""
        rel, abs_path = found
        try:
            raw = read_text_file(abs_path)
        except (FileNotFoundError, PermissionError, OSError, UnicodeDecodeError) as exc:
            logger.warning(f"workspace_instruction_read_failed path={abs_path} error={exc}")
            return ""
        # 窗口比例闸门：0 表示仅用固定上限；否则与窗口比例上限取 min。
        max_total = Settings.WORKSPACE_INSTRUCTION_MAX_TOTAL_TOKENS
        if Settings.WORKSPACE_INSTRUCTION_WINDOW_RATIO > 0:
            window_cap = int(
                Settings.CONTEXT_WINDOW_TOKENS * Settings.WORKSPACE_INSTRUCTION_WINDOW_RATIO
            )
            max_total = min(max_total, window_cap)
        content = SystemPromptBuilder._enforce_budget(
            raw,
            Settings.WORKSPACE_INSTRUCTION_MAX_FILE_BYTES,
            min(Settings.WORKSPACE_INSTRUCTION_MAX_FILE_TOKENS, max_total),
        )
        return f"<workspace_layer>\n# ./{rel.as_posix()}\n{content}\n</workspace_layer>"

    @staticmethod
    def _find_instruction_file(
        root: Path, file_names: Sequence[str], max_depth: int
    ) -> tuple[Path, Path] | None:
        """定位优先级最高的唯一项目指令文件（只定位，不读取内容）。

        优先级键（升序取最小）：
            1. ``file_names`` 下标：越靠前越优先（``AGENTS.md`` 优先于 ``CLAUDE.md``）；
            2. 文件所在目录层级：根目录文件最优先，越浅越优先；
            3. 相对路径字典序：同层级多命中时的确定性兜底。
        文件名比较不区分大小写。

        采用 BFS 逐层扫描 + 在线维护最优项 + 逐层剪枝：
            - 不收集全部命中、不排序、不读取文件内容；
            - 每处理完一个完整目录层级再判定收工：一旦最优项的文件名下标为 0，更深层
              不可能更优，立即结束（根目录命中时只需扫描根目录一层）；
            - 跳过 ``_IGNORED_DIRS`` 与超过 ``max_depth`` 的目录，不跟随符号链接；
            - 相对路径由绝对路径做纯字符串换算得到，避免逐候选 ``resolve()`` 的额外系统调用。

        参数:
            root: 工作区根目录（内部解析为绝对路径）。
            file_names: 目标文件名序列，**顺序即优先级**。
            max_depth: 最大扫描深度（0 表示仅根目录直接文件）。

        返回:
            最优文件的 ``(相对路径, 绝对路径)``；工作区内无任何命中时返回 ``None``。

        异常:
            无（目录不可读、路径异常均静默跳过）。

        副作用:
            无（仅遍历目录，不读取文件内容）。
        """
        priority = {name.lower(): index for index, name in enumerate(file_names)}
        try:
            root = root.resolve()
        except OSError:
            return None
        if not priority or not root.is_dir():
            return None

        best_key: tuple[int, int, str] | None = None
        best_rel: Path | None = None
        best_abs: Path | None = None
        queue: deque[tuple[Path, int]] = deque([(root, 0)])

        while queue:
            for _ in range(len(queue)):  # 一次迭代覆盖一个完整目录层级
                directory, depth = queue.popleft()
                try:
                    with os.scandir(directory) as entries:
                        for e in entries:
                            index = priority.get(e.name.lower())
                            if index is not None:
                                try:
                                    rel = Path(e.path).relative_to(root)
                                except ValueError:
                                    continue
                                key = (index, len(rel.parts), rel.as_posix())
                                if best_key is None or key < best_key:
                                    best_key = key
                                    best_rel = rel
                                    best_abs = Path(e.path)
                            elif (
                                depth + 1 <= max_depth
                                and e.name not in _IGNORED_DIRS
                                and e.is_dir(follow_symlinks=False)
                            ):
                                queue.append((Path(e.path), depth + 1))
                except (PermissionError, OSError):
                    continue
            if best_key is not None and best_key[0] == 0:
                break  # 已选中最优先文件名，更深层不可能更优

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
