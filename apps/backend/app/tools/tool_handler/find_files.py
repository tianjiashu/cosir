"""find_files 模型工具适配层。"""

from __future__ import annotations

import time

from app.tools.display.filesystem_display import build_file_search_display_data
from app.core.tool.schemas import (
    TOOL_FIND_FILES,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.tool_grouping import TOOL_GROUP_SEARCH
from app.tools.tool_handler.search.errors import (
    SearchCancelled,
    SearchPathNotFound,
    SearchPathUnreadable,
    SearchTimedOut,
)
from app.tools.tool_handler.search.filename_engine import find_files
from app.tools.tool_handler.search.scope import SearchScope
from app.tools.tool_handler.security.path_resolver import PathResolver
from app.core.tool.tool_base import HandlerBase
from app.tools.tool_models.find_files_args import FindFilesArgs

FIND_FILES_DESCRIPTION = (
    "Find files by filename or workspace-relative glob. The path can be a file or directory "
    "relative to the workspace; a directory is searched recursively. Use search_content to "
    "search inside file contents."
)


class FindFilesTool(HandlerBase):
    """只读按文件名 glob 查找文件的模型工具。"""

    name = TOOL_FIND_FILES
    description = FIND_FILES_DESCRIPTION
    args_model = FindFilesArgs
    timeout_seconds = 5.0
    group = TOOL_GROUP_SEARCH


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
            )
        resolved_path, path_error = resolver.resolve_without_boundary(path)
        if resolved_path is None:
            return tool_error(
                self.name,
                f"could not search path: {path_error or path}. "
                "Provide an existing file or directory path relative to the workspace.",
            )
        try:
            scope = SearchScope.from_path(
                execution_context.workspace_root, resolved_path, execution_context.task_id, time.monotonic() + self.timeout_seconds
            )
            device_error = resolver.blocked_recursive_search_reason(path, resolved_path)
            if device_error:
                return tool_error(
                    self.name,
                    device_error,
                )
            page = find_files(
                scope,
                pattern,
                limit=limit,
                offset=offset,
            )
        except SearchCancelled:
            return tool_cancelled(
                self.name,
                reason="user cancelled the search",
            )
        except SearchTimedOut:
            return tool_error(
                self.name,
                "file search exceeded its time limit. "
                "Narrow the path or pattern and call find_files again.",
                status_hint="搜索超时",
            )
        except SearchPathNotFound as exc:
            return tool_error(
                self.name,
                f"could not search path: {exc}. "
                "Provide an existing file or directory path relative to the workspace.",
            )
        except SearchPathUnreadable as exc:
            return tool_error(
                self.name,
                f"could not search path: {exc}. "
                "Provide a readable file or directory path.",
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
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
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
