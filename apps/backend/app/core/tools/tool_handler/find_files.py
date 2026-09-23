"""find_files 模型工具适配层。"""

from __future__ import annotations

import time

from app.core.tools.display.filesystem_display import build_file_search_display_data
from app.core.tools.schemas import (
    TOOL_FIND_FILES,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import blocked_device_reason, tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.search.errors import (
    SearchPathNotFound,
    SearchPathUnreadable,
    SearchTimedOut,
)
from app.core.tools.tool_handler.search.filename_engine import find_files
from app.core.tools.tool_handler.search.scope import SearchScope
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.find_files_args import FindFilesArgs

FIND_FILES_DESCRIPTION = (
    "Find files by filename or workspace-relative glob. The path can be a file or directory "
    "relative to the workspace; a directory is searched recursively. Use search_content to "
    "search inside file contents."
)


class FindFilesTool(HandlerBase):
    """只读按文件名 glob 查找文件的模型工具。"""

    name = TOOL_FIND_FILES
    description = FIND_FILES_DESCRIPTION
    permission = "file_search"
    args_model = FindFilesArgs
    timeout_seconds = 30.0
    risk_level = "low"

    def execute(
        self,
        pattern: str,
        path: str,
        execution_context: ToolExecutionContext,
        limit: int = 50,
        offset: int = 0,
    ) -> ToolObservation:
        """解析文件/目录 scope，执行文件名搜索并收口为 ToolObservation。"""

        resolver = PathResolver(execution_context.workspace_root)
        device_error = resolver.blocked_device_reason(path)
        if device_error:
            return tool_error(
                self.name,
                device_error,
                reason=blocked_device_reason("searched"),
                permission=self.permission,
            )
        resolved_path, path_error = resolver.resolve_without_boundary(path)
        if resolved_path is None:
            return tool_error(
                self.name,
                f"could not search path: {path_error or path}",
                reason="provide an existing file or directory path relative to the workspace.",
                retryable=True,
                permission=self.permission,
            )
        try:
            scope = SearchScope.from_path(execution_context.workspace_root, resolved_path)
            device_error = resolver.blocked_recursive_search_reason(path, resolved_path)
            if device_error:
                return tool_error(
                    self.name,
                    device_error,
                    reason=blocked_device_reason("searched"),
                    permission=self.permission,
                )
            page = find_files(
                scope,
                pattern,
                limit=limit,
                offset=offset,
                deadline=time.monotonic() + self.timeout_seconds,
            )
        except SearchTimedOut:
            return tool_error(
                self.name,
                "file search exceeded its time limit",
                reason="narrow the path or pattern and call find_files again.",
                retryable=True,
                permission=self.permission,
                status_hint="搜索超时",
            )
        except SearchPathNotFound as exc:
            return tool_error(
                self.name,
                f"could not search path: {exc}",
                reason="provide an existing file or directory path relative to the workspace.",
                retryable=True,
                permission=self.permission,
            )
        except SearchPathUnreadable as exc:
            return tool_error(
                self.name,
                f"could not search path: {exc}",
                reason="provide a readable file or directory path.",
                retryable=True,
                permission=self.permission,
            )

        paths = [item.file_path for item in page.matches]
        content = "\n".join(paths)
        if page.total_files > offset + limit:
            content += (
                f"\n\n[Hint: Results truncated ({page.total_files} total files). "
                f"Use offset={offset + limit} to see more, or narrow the pattern or path.]"
            )
        if not content:
            content = "No files found."
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=content,
            display_data=build_file_search_display_data(
                pattern=pattern,
                path=path,
                files=paths,
                offset=offset,
                limit=limit,
                match_count=page.total_files,
            ),
        )

    def to_definition(self) -> ToolDefinition:
        """返回注册用的 find_files 定义；执行保持在线程内。"""

        return ToolDefinition(
            name=self.name,
            description=self.description,
            permission=self.permission,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            resource_keys=("filesystem",),
            execution_mode="thread",
            display=ToolDisplayHints(
                verb="查找文件",
                icon="search",
                expandable=True,
                expand_layout="list",
                show_result=False,
            ),
        )


def build_find_files_definition() -> ToolDefinition:
    """构造 find_files 的注册定义。"""

    return FindFilesTool().to_definition_if_avaliable()
