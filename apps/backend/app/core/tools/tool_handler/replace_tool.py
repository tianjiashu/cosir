"""replace 工具实现（从原合并 patch_tool 的 replace 模式平移）。

本模块只承载 patch（replace 模式）这一个工具：单文件模糊查找替换，复刻原
edit_file 逻辑。成功后返回 unified diff 回显（``content``）与结构化 diff 统计
（``data["diff_stats"]``），对齐 Hermes ``patch_tool`` 的 replace 分支。落盘后经
``guard.syntax_check`` 做多语言语法检查（error 驱动）：命中语法错误返回 error 观察
（文件已写），经 ``reason`` 引导 Agent 二次编辑覆盖自修复。

设计边界：
- 路径安全委托 ``security.ProjectPathResolver``。
- replace 的模糊匹配复用 ``patch.fuzzy_match``。
- 成功/失败观察统一经 ``tool_execute.tool_success`` / ``tool_error`` 工厂构造。
- 语法检查委托 ``guard.syntax_check``（多语言单一来源），不内联校验。
"""

import dataclasses
from pathlib import Path

from app.core.tools.guard.syntax_check import check_source_syntax, format_syntax_reason
from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import (
    blocked_device_reason,
    os_error_message,
    tool_error,
)
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.file_io.atomic_write import atomic_write_text, looks_like_line_numbered
from app.core.tools.tool_handler.patch import (
    format_no_match_hint,
    format_patch_diff,
    fuzzy_find_and_replace,
)
from app.core.tools.tool_handler.patch.file_change_display import (
    build_file_change_display_data,
)
from app.core.tools.tool_handler.patch.patch_diff import FileDiffResult
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.replace_args import ReplaceArgs

REPLACE_DESCRIPTION = (
    "Targeted find-and-replace edits in files. Use this instead of sed/awk in terminal. "
    "Uses fuzzy matching (9 strategies) so minor whitespace/indentation "
    "differences won't break it. "
    "Returns a unified diff. Auto-runs syntax checks after editing.\n\n"
    "REPLACE MODE: find a unique string and replace it. "
    "REQUIRED PARAMETERS: path, old_string, new_string (replace_all is optional)."
)


class ReplaceTool(HandlerBase):
    """在文件内做查找替换的工具类（原 patch 工具的 replace 模式）。

    参数:
        无。

    返回:
        ``ReplaceTool`` 实例。

    异常:
        初始化阶段不主动抛出业务异常。

    副作用:
        仅保存工具元数据；不读取、不写入文件。
    """

    name = "patch"
    description = REPLACE_DESCRIPTION
    permission = "file_write"
    args_model = ReplaceArgs
    timeout_seconds = 30.0
    risk_level = "medium"

    def __init__(self) -> None:
        """初始化 replace 工具实例。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            仅保存工具元数据，不执行文件系统操作。
        """

    def execute(
        self,
        execution_context: ToolExecutionContext,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> ToolObservation:
        """在单文件内做模糊查找替换，并统一收口成功/失败观察。

        参数:
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；
                由执行链在执行期强制注入，handler 契约必须接受此 kwarg。
                破坏性操作以其 ``workspace_root`` 作为路径 containment 的唯一事实源。
            path: 目标文件相对路径（必填）。
            old_string: 待查找文本（必填）。
            new_string: 替换文本（必填，空串表示删除匹配文本）。
            replace_all: 是否替换所有命中（默认 False，要求唯一命中）。

        返回:
            ``ToolObservation``：成功时 ``content`` 为 unified diff 回显、
            ``data["diff_stats"]`` 为结构化统计；失败时 ``status`` 为 error，
            ``error``/``reason`` 提供面向模型的
            富文本诊断（``error``=发生了什么、``reason``=为什么失败+如何修正+是否重试）。

        异常:
            不主动向上抛出；所有失败路径均归一化为错误观察。

        副作用:
            命中时原子回写目标文件。
        """
        resolver = PathResolver(execution_context.workspace_root)
        if not path or old_string is None or new_string is None:
            return tool_error(
                self.name,
                "replace requires path, old_string, new_string",
                reason=(
                    "replace mode needs all of path, old_string and new_string; at least "
                    "one is missing. Provide a target file path, the exact text to find, "
                    "and its replacement; the same incomplete arguments will always fail."
                ),
                permission=self.permission,
            )
        if old_string == new_string:
            return tool_error(
                self.name,
                "old_string and new_string are identical, no changes would be made",
                reason=(
                    "old_string and new_string are exactly the same, so this edit is a "
                    "no-op and the file would not change. This is deterministic: provide "
                    "a new_string that differs from old_string, or skip the call if no "
                    "change is actually needed."
                ),
                permission=self.permission,
            )
        device_error = resolver.blocked_device_reason(path)
        if device_error:
            return tool_error(
                self.name,
                device_error,
                reason=blocked_device_reason("patched"),
                permission=self.permission,
            )
        resolved, error = resolver.resolve_within_workspace(path)
        if resolved is None:
            return tool_error(
                self.name,
                f"could not patch the file: {error}",
                reason=(
                    "the path escapes the project workspace and cannot be patched. The "
                    "resolver rejects paths that point outside the workspace root for "
                    "safety. Pass a path inside the project (relative to the workspace "
                    "root, or an absolute path under it); the same out-of-bounds path will "
                    "always be rejected."
                ),
                permission=self.permission,
            )
        device_error = resolver.blocked_device_reason(path, resolved)
        if device_error:
            return tool_error(
                self.name,
                device_error,
                reason=blocked_device_reason("patched"),
                permission=self.permission,
            )
        try:
            original = Path(resolved).read_text(encoding="utf-8")
        except OSError as exc:
            return tool_error(
                self.name,
                os_error_message(exc, "read the file"),
                reason=(
                    "the file could not be read before patching, usually because it is "
                    "locked by another process or the current user lacks read permission. "
                    "Close the program holding the file or adjust permissions, then retry "
                    "the same patch."
                ),
                retryable=True,
                permission=self.permission,
            )
        new_content, count, _, match_error = fuzzy_find_and_replace(
            original, old_string, new_string, replace_all
        )
        if count == 0:
            message = match_error or "Could not find a match for old_string in the file"
            message += format_no_match_hint(match_error, count, old_string, original)
            return tool_error(
                self.name,
                message,
                reason=(
                    "old_string was not found in the file (or occurred more than once "
                    "without replace_all). Show the exact current text via read_file and "
                    "retry with an old_string that matches uniquely; the same old_string "
                    "will always fail until the file content changes."
                ),
                permission=self.permission,
            )
        if looks_like_line_numbered(new_content):
            return tool_error(
                self.name,
                "resulting content appears line-numbered; remove 'N| ' prefixes first.",
                reason=(
                    "the patched content looks like line-numbered read_file output (most "
                    "lines start with a 'N| ' prefix). These prefixes are display metadata, "
                    "not file content. Strip the 'N| ' prefix from the new_string and retry; "
                    "the same content will always be rejected."
                ),
                permission=self.permission,
            )
        try:
            atomic_write_text(
                resolved,
                new_content,
                containment_root=resolver.workspace_root,
            )
        except OSError as exc:
            return tool_error(
                self.name,
                os_error_message(exc, "write the file"),
                reason=(
                    "the patched file could not be written, usually because it is locked "
                    "by another process or the current user lacks write permission. Close "
                    "the program holding the file or adjust permissions, then retry the "
                    "same patch."
                ),
                retryable=True,
                permission=self.permission,
            )
        # 落盘后语法检查（error 驱动）：命中语法错误返回 error 观察（文件已写），
        # 经 reason 引导 Agent 二次编辑覆盖自修复。
        result = check_source_syntax(str(resolved), new_content)
        if result.has_error:
            return tool_error(
                tool_name=self.name,
                error="syntax error detected after patch",
                reason=format_syntax_reason(result),
                permission=self.permission,
                display_data={"syntax_errors": [dataclasses.asdict(d) for d in result.diagnostics]},
            )
        snapshot = FileDiffResult(path=path, status="modified", before=original, after=new_content)
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=format_patch_diff([snapshot]),
            data=build_file_change_display_data([snapshot]),
        )

    def to_definition(self) -> ToolDefinition:
        """把工具实例转换成 ``ToolDefinition``。

        参数:
            无。

        返回:
            可直接注册到 ``ToolRegistry`` 的工具定义（含 display 展示元数据）。

        异常:
            无。

        副作用:
            无。
        """

        return ToolDefinition(
            name=self.name,
            description=self.description,
            permission=self.permission,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            resource_keys=("filesystem",),
            display=ToolDisplayHints(
                verb="",
                icon="git-compare",
                expandable=True,
                expand_layout="diff",
            ),
        )


def build_replace_definition() -> ToolDefinition:
    """构造 replace（patch 工具）工具定义。

    参数:
        无。

    返回:
        ``ToolDefinition``，供 ``ToolRegistry`` 注册。

    异常:
        无。

    副作用:
        创建 ``ReplaceTool`` 实例和定义对象，不执行文件操作。
    """

    return ReplaceTool().to_definition()
