"""构建面向模型的系统提示词（三层结构）。

系统提示词重构为三层，每层一块，边界清晰：

1. ``<runtime_context>`` 动态变量层：运行期才确定的事实（角色、工作区、日期、工具集合、
   语言、CodeGraph 开关等），直接由 ``AgentProfile`` / ``Settings`` / 系统状态注入，不读取任何文件。
2. ``<agent_layer>`` Agent 系统预设层：来源唯一为 ``AgentProfile.prompt_file_path`` 指向的
   md/txt 文件全文（系统预设，与用户无关）；该字段为 ``None`` 或文件读取失败时使用空的
   规则层，加载后不做变量替换。
3. ``<workspace_layer>`` Workspace 项目层：扫描工作区下的项目指令文件（如 ``AGENTS.md`` /
   ``CLAUDE.md``），受预算闸门约束，避免上下文爆炸。

预算控制仅在 Layer 2（单文件上限）与 Layer 3（单文件 / 文件数 / 总量 token + 窗口比例）生效；
Layer 1 内容小且固定，仅给字节硬上限防御。
"""

from __future__ import annotations

import logging
import os
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

    # --- Layer 3: Workspace 项目指令层（扫描 + 预算闸门） ---
    @staticmethod
    def _build_workspace_layer(workspace_root: str) -> str:
        """构建 Workspace 项目指令层：扫描并聚合项目指令文件，受预算闸门约束。

        参数:
            workspace_root: 当前工作区根目录。

        返回:
            包裹在 ``<workspace_layer>`` 标签内的聚合文本；无文件时返回占位说明。

        异常:
            无（扫描/读取失败均容错，记日志并跳过）。

        副作用:
            遍历并读取 workspace 下的项目指令文件。
        """
        files = SystemPromptBuilder._collect_instruction_files(
            Path(workspace_root),
            set(Settings.WORKSPACE_INSTRUCTION_FILE_NAMES),
            Settings.WORKSPACE_INSTRUCTION_MAX_DEPTH,
        )
        max_files = Settings.WORKSPACE_INSTRUCTION_MAX_FILES
        max_file_bytes = Settings.WORKSPACE_INSTRUCTION_MAX_FILE_BYTES
        max_file_tokens = Settings.WORKSPACE_INSTRUCTION_MAX_FILE_TOKENS
        max_total = Settings.WORKSPACE_INSTRUCTION_MAX_TOTAL_TOKENS
        # 窗口比例闸门：0 表示仅用固定上限；否则与窗口比例上限取 min。
        if Settings.WORKSPACE_INSTRUCTION_WINDOW_RATIO > 0:
            window_cap = int(
                Settings.CONTEXT_WINDOW_TOKENS * Settings.WORKSPACE_INSTRUCTION_WINDOW_RATIO
            )
            max_total = min(max_total, window_cap)

        blocks: list[str] = []
        total_tokens = 0
        omitted_files = 0
        scanned = 0
        for rel, abs_path in files:
            if scanned >= max_files:
                omitted_files += len(files) - scanned
                break
            scanned += 1
            try:
                size = abs_path.stat().st_size
            except OSError:
                size = 0
            if size > max_file_bytes:
                logger.info(
                    f"workspace_instruction_file_skipped_too_large "
                    f"path={abs_path} bytes={size}"
                )
                omitted_files += 1
                continue
            try:
                raw = abs_path.read_text(encoding="utf-8")
            except (FileNotFoundError, PermissionError, OSError) as exc:
                logger.warning(
                    f"workspace_instruction_read_failed path={abs_path} error={exc}"
                )
                omitted_files += 1
                continue
            content = SystemPromptBuilder._truncate_tokens(raw, max_file_tokens)
            est = TokenEstimator.estimate(content)
            if total_tokens + est > max_total:
                # 超出总量上限：舍弃本文件及剩余全部。
                omitted_files += len(files) - scanned + 1
                break
            total_tokens += est
            blocks.append(f"# ./{rel.as_posix()}\n{content}")

        if not blocks:
            return ""
        header = "<workspace_layer>"
        if omitted_files:
            header += f"\n（已省略 {omitted_files} 个文件以控制上下文预算）"
        return header + "\n" + "\n\n".join(blocks) + "\n</workspace_layer>"

    @staticmethod
    def _collect_instruction_files(
        root: Path, file_names: set[str], max_depth: int
    ) -> list[tuple[Path, Path]]:
        """按深度优先遍历收集匹配指令文件，按（深度升序, 路径字典序）排序。

        参数:
            root: 工作区根目录（解析为绝对路径）。
            file_names: 目标文件名集合（如 ``{"AGENTS.md", "CLAUDE.md"}``）。
            max_depth: 最大扫描深度（0 表示仅根目录直接文件）。

        返回:
            ``(相对路径, 绝对路径)`` 列表，已按深度升序、路径字典序排序。

        异常:
            无（目录不可读时静默跳过）。

        副作用:
            无（仅遍历，不读取文件内容）。
        """
        found: list[tuple[Path, Path]] = []
        try:
            root = root.resolve()
        except OSError:
            return found

        def walk(d: Path, depth: int) -> None:
            if depth > max_depth:
                return
            try:
                entries = list(os.scandir(d))
            except (PermissionError, OSError):
                return
            for e in sorted(entries, key=lambda x: x.name):
                if e.is_dir(follow_symlinks=False):
                    if e.name in _IGNORED_DIRS:
                        continue
                    walk(Path(e.path), depth + 1)
                elif e.is_file(follow_symlinks=False):
                    if e.name in file_names:
                        try:
                            rel = Path(e.path).resolve().relative_to(root)
                        except ValueError:
                            continue
                        found.append((rel, Path(e.path)))

        if root.exists():
            walk(root, 0)
        found.sort(key=lambda x: (len(x[0].parts), x[0].as_posix()))
        return found

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
