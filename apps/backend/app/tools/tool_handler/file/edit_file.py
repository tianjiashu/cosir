"""edit_file：在已有文件里做原文替换。

修改前校验模型已读版本，匹配原文后原子写入。结果携带确定的变更事实与展示 diff。
"""

from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.results.tool_error import os_error_message, tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_EDIT_FILE,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.schemas.tool_grouping import TOOL_GROUP_FILE_EDIT
from app.core.tool.tool_base import HandlerBase
from app.core.workflow.cancel.run_cancellation_registry import cancellation_registry
from app.tools.display.file_change_display import build_file_change_display_data
from app.tools.file_system.facts.file_facts import FileFacts
from app.tools.file_system.facts.file_mutation import FileMutation
from app.tools.file_system.facts.file_version import FileVersion
from app.tools.file_system.path_resolver import PathResolver
from app.tools.file_system.write_attribution import write_attribution
from app.tools.file_system.write_policy import WriteRefused, WriteTarget, file_write_policy
from app.tools.tool_handler.file.file_edit.line_numbered import looks_like_line_numbered
from app.tools.tool_handler.file.file_edit.patch_diff import FileDiffResult
from app.tools.tool_handler.file.file_edit.syntax_check import (
    check_source_syntax,
    format_syntax_reason,
)
from app.tools.tool_handler.file.file_edit.text_match import TextMatchError, replace_exact
from app.tools.tool_models.edit_file_args import EditFileArgs
from app.utils.atomic_write import atomic_write_text

EDIT_FILE_DESCRIPTION = (
    "Read the current file before calling this tool. Performs exact string replacement.\n"
    "\n"
    "- `old_string` must match the file exactly, including indentation, and be unique — the edit "
    'fails otherwise. Strip the read_file line prefix (line number + "| ") before matching.\n'
    "- `replace_all: true` replaces every occurrence instead."
)


class EditFileTool(HandlerBase):
    name = TOOL_EDIT_FILE
    description = EDIT_FILE_DESCRIPTION
    args_model = EditFileArgs
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
            return tool_error(
                self.name,
                "old_string is empty. Provide the exact text to replace.",
                status_hint="原文为空",
            )
        if old_string == new_string:
            return tool_error(
                self.name,
                "old_string and new_string are identical, no changes would be made. "
                "Provide a different new_string, or skip the call if no change is needed.",
                status_hint="无需改动",
            )
        resolver = PathResolver(
            execution_context.workspace_root,
            execution_context.writable_root,
            execution_context.scratch_dir,
        )
        device_error = resolver.blocked_device_reason(path)
        if device_error:
            return tool_error(self.name, device_error, status_hint="路径不可写")
        resolved, error = resolver.resolve_for_write(path)
        if resolved is None:
            return tool_error(
                self.name, f"could not edit the file: {error}.", status_hint="路径不可写"
            )
        device_error = resolver.blocked_device_reason(path, resolved)
        if device_error:
            return tool_error(self.name, device_error, status_hint="路径不可写")
        before = FileVersion.of(resolved)
        target = WriteTarget(resolved, "modify", before)
        try:
            with file_write_policy.reserve(
                (target,), execution_context
            ):
                original = resolved.read_text(encoding="utf-8")
                new_content, _ = replace_exact(
                    original, old_string, new_string, replace_all=replace_all
                )
                if looks_like_line_numbered(new_content):
                    return tool_error(
                        self.name,
                        "resulting content appears line-numbered. Remove the 'N| ' display prefixes.",
                        status_hint="内容带行号",
                    )
                if cancellation_registry.is_cancelled(execution_context.session_id):
                    return tool_cancelled(tool_name=self.name)
                atomic_write_text(
                    resolved, new_content, containment_root=resolver.write_containment(resolved)
                )
                written = FileFacts(
                    mutations=(
                        FileMutation(
                            path=str(resolved),
                            operation="modify",
                            before=before,
                            after=FileVersion.of(resolved),
                        ),
                    )
                )
                write_attribution.record(execution_context.session_id, written)
                facts = written.as_result()
        except WriteRefused as exc:
            return tool_error(self.name, str(exc), status_hint=exc.hint)
        except TextMatchError as exc:
            return tool_error(
                self.name,
                f"could not edit the file: {exc}. Read the current file, then retry with its exact text.",
                status_hint=exc.status_hint,
            )
        except (OSError, UnicodeDecodeError) as exc:
            detail = (
                os_error_message(exc, "edit the file")
                if isinstance(exc, OSError)
                else "file is not UTF-8"
            )
            return tool_error(self.name, detail, status_hint="无法编辑")
        display_data = build_file_change_display_data(
            [FileDiffResult(path=path, status="modified", before=original, after=new_content)]
        )
        result = check_source_syntax(str(resolved), new_content)
        return tool_success(
            tool_name=self.name,
            content=("success\nsyntax warning:\n" + format_syntax_reason(result))
            if result.has_error
            else None,
            display_data=display_data,
            facts=facts,
        )

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
        )


def build_edit_file_definition() -> ToolDefinition:
    return EditFileTool().to_definition()
