"""edit_file：在已有文件里做原文替换。

描述照搬 Claude Code 的 Edit 工具，只改 cosir 不同之处（相对路径、``read_file`` 的 ``N| `` 行号前缀、
工具名）。不要求先读过文件：``old_string`` 必须与原文完全一致，本身就证明模型看过要改的那段，文件被
外部改过时原文对不上、调用失败；会丢内容的整文件覆盖由 ``write_file`` 的读取记录守着。成功后把文件
当前状态记进读取记录（``file_observations``），之后可以直接用 ``write_file`` 覆盖。文件变更经
``display_data`` 展示，不回显 diff 给模型；落盘后的语法问题只作为警告附在成功结果里。
"""

from pathlib import Path

from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.results.tool_error import os_error_message, tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_EDIT_FILE,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_FILE_EDIT
from app.core.workflow.cancel.run_cancellation_registry import cancellation_registry
from app.tools.display.file_change_display import build_file_change_display_data
from app.tools.guard.syntax_check import check_source_syntax, format_syntax_reason
from app.tools.tool_handler.file_edit.line_numbered import looks_like_line_numbered
from app.tools.tool_handler.file_edit.patch_diff import FileDiffResult
from app.tools.tool_handler.file_edit.text_match import TextMatchError, replace_exact
from app.tools.tool_handler.file_observations import file_observations
from app.tools.tool_handler.security.path_resolver import PathResolver
from app.tools.tool_models.edit_file_args import EditFileArgs
from app.utils.atomic_write import atomic_write_text

EDIT_FILE_DESCRIPTION = (
    "Performs exact string replacement in an existing file.\n"
    "\n"
    "- `old_string` must match the file exactly, including indentation, and be unique — the edit "
    "fails otherwise. Strip the read_file line prefix (line number + \"| \") before matching.\n"
    "- `replace_all: true` replaces every occurrence instead."
)


class EditFileTool(HandlerBase):
    name = TOOL_EDIT_FILE
    description = EDIT_FILE_DESCRIPTION
    args_model = EditFileArgs
    timeout_seconds = 30.0
    group = TOOL_GROUP_FILE_EDIT

    def execute(
        self,
        execution_context: ToolExecutionContext,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> ToolObservation:
        """替换并原子回写；所有失败都收口为错误观察，不向上抛。相对路径按工作区根解析，写入边界是可写根。"""

        if not old_string:
            return tool_error(self.name, "old_string is empty. Provide the exact text to replace.")
        if old_string == new_string:
            return tool_error(
                self.name,
                "old_string and new_string are identical, no changes would be made. "
                "Provide a different new_string, or skip the call if no change is needed.",
            )
        resolver = PathResolver(execution_context.workspace_root, execution_context.writable_root)
        device_error = resolver.blocked_device_reason(path)
        if device_error:
            return tool_error(self.name, device_error)
        resolved, error = resolver.resolve_for_write(path)
        if resolved is None:
            return tool_error(self.name, f"could not edit the file: {error}.")
        device_error = resolver.blocked_device_reason(path, resolved)
        if device_error:
            return tool_error(self.name, device_error)
        try:
            # read_text 按通用换行读入（\r\n → \n），与 read_file 给模型看的一致；写回时
            # atomic_write_text 按原文件还原换行风格。
            original = Path(resolved).read_text(encoding="utf-8")
        except OSError as exc:
            return tool_error(
                self.name,
                f"{os_error_message(exc, 'read the file')}. Make the file readable, then call edit_file again.",
            )
        try:
            new_content, _ = replace_exact(original, old_string, new_string, replace_all=replace_all)
        except TextMatchError as exc:
            return tool_error(
                self.name,
                f"could not edit the file: {exc}. Read the current file with read_file, then retry "
                "with old_string copied exactly from it.",
            )
        if looks_like_line_numbered(new_content):
            return tool_error(
                self.name,
                "resulting content appears line-numbered. Remove the 'N| ' display prefixes from new_string.",
            )
        if cancellation_registry.is_cancelled(execution_context.session_id):
            return tool_cancelled(tool_name=self.name)
        try:
            atomic_write_text(resolved, new_content, containment_root=resolver.writable_root)
        except OSError as exc:
            return tool_error(
                self.name,
                f"{os_error_message(exc, 'write the file')}. Make the file writable, then call edit_file again.",
            )
        file_observations.record(execution_context.session_id, resolved)
        display_data = build_file_change_display_data(
            [FileDiffResult(path=path, status="modified", before=original, after=new_content)]
        )
        result = check_source_syntax(str(resolved), new_content)
        return tool_success(
            tool_name=self.name,
            content=("success\nsyntax warning:\n" + format_syntax_reason(result)) if result.has_error else None,
            display_data=display_data,
        )

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            resource_keys=("filesystem",),
        )


def build_edit_file_definition() -> ToolDefinition:
    return EditFileTool().to_definition_if_avaliable()
