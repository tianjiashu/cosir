"""replace 工具实现（从原合并 patch_tool 的 replace 模式平移）。

本模块只承载 patch_write（replace 模式）这一个工具：单文件模糊查找替换，复刻原
edit_file 逻辑。成功后的文件变更展示由 display_data 提供，不把 diff 回显给模型。落盘后的
语法检查只在发现问题时通过 success content 提供
简短警告，不改变替换成功状态。

设计边界：
- 路径安全委托 ``security.ProjectPathResolver``。
- replace 的模糊匹配复用 ``patch_write.fuzzy_match``。
- 成功/失败观察统一经 ``tool_execute.tool_success`` / ``tool_error`` 工厂构造。
- 语法检查委托 ``guard.syntax_check``（多语言单一来源），不内联校验。
"""

from pathlib import Path

from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.tools.display.file_change_display import build_file_change_display_data
from app.core.tools.guard.syntax_check import check_source_syntax, format_syntax_reason
from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_cancelled import tool_cancelled
from app.core.tools.tool_execute.tool_error import (
    blocked_device_reason,
    os_error_message,
    tool_error,
)
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.patch_write.atomic_write import (
    atomic_write_text,
    looks_like_line_numbered,
)
from app.core.tools.tool_handler.patch_write import (
    format_no_match_hint,
    fuzzy_find_and_replace,
)
from app.core.tools.tool_handler.patch_write.patch_diff import FileDiffResult
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.replace_args import ReplaceArgs

REPLACE_DESCRIPTION = (
    "Targeted find-and-replace edits in files. Use this instead of sed/awk in terminal. "
    "Uses fuzzy matching (9 strategies) so minor whitespace/indentation "
    "differences won't break it. "
    "Reports the file change in the tool UI. Auto-runs syntax checks after editing.\n\n"
    "REPLACE MODE: find a unique string and replace it. "
    "REQUIRED PARAMETERS: path, old_string, new_string (replace_all is optional)."
)


class ReplaceTool(HandlerBase):
    """在文件内做查找替换的工具类（原 patch_write 工具的 replace 模式）。

    参数:
        无。

    返回:
        ``ReplaceTool`` 实例。

    异常:
        初始化阶段不主动抛出业务异常。

    副作用:
        仅保存工具元数据；不读取、不写入文件。
    """

    name = "patch_write"
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
            ``ToolObservation``：成功时 content 为空，除非语法检查发现问题并返回
            简短警告；文件变更通过 display_data 提供。失败时
            ``error`` 描述事实，``reason`` 提供下一步动作。

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
                reason="provide path, old_string, and new_string, then call patch_write again.",
                retryable=True,
                permission=self.permission,
            )
        if old_string == new_string:
            return tool_error(
                self.name,
                "old_string and new_string are identical, no changes would be made",
                reason="provide a different new_string, or skip the call if no change is needed.",
                retryable=True,
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
                f"could not patch_write the file: {error}",
                reason="provide a file path inside the project workspace.",
                retryable=True,
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
                reason="make the file readable, then call patch_write again.",
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
                    "read the current file and provide an old_string with a unique match, "
                    "or set replace_all=true."
                ),
                retryable=True,
                permission=self.permission,
            )
        if looks_like_line_numbered(new_content):
            return tool_error(
                self.name,
                "resulting content appears line-numbered",
                reason="remove the 'N| ' display prefixes from new_string.",
                retryable=True,
                permission=self.permission,
            )
        try:
            if cancellation_registry.is_cancelled(execution_context.run_id):
                return tool_cancelled(
                    tool_name=self.name,
                    permission=self.permission,
                )
            atomic_write_text(
                resolved,
                new_content,
                containment_root=resolver.workspace_root,
            )
        except OSError as exc:
            return tool_error(
                self.name,
                os_error_message(exc, "write the file"),
                reason="make the file writable, then call patch_write again.",
                retryable=True,
                permission=self.permission,
            )
        # 落盘后语法检查（error 驱动）：命中语法错误返回 error 观察（文件已写），
        # 经 reason 引导 Agent 二次编辑覆盖自修复。
        snapshot = FileDiffResult(path=path, status="modified", before=original, after=new_content)
        display_data = build_file_change_display_data([snapshot])
        result = check_source_syntax(str(resolved), new_content)
        if result.has_error:
            return tool_success(
                tool_name=self.name,
                permission=self.permission,
                content=(
                    "success\nsyntax warning:\n"
                    + format_syntax_reason(result)
                ),
                display_data=display_data,
            )
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=None,
            display_data=display_data,
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
                verb="替换文本",
                icon="git-compare",
                surface="standalone",
                expandable=True,
                expand_layout="diff",
                show_result=False,
            ),
        )


def build_replace_definition() -> ToolDefinition:
    """构造 replace（patch_write 工具）工具定义。

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
