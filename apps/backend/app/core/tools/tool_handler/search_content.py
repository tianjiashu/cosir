"""search_content 模型工具适配层。"""

from __future__ import annotations

import time
from typing import Any

from app.core.tools.display.filesystem_display import build_content_search_display_data
from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import blocked_device_reason, tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.search.content_engine import search_content
from app.core.tools.tool_handler.search.errors import (
    InvalidSearchPattern,
    SearchPathNotFound,
    SearchPathUnreadable,
    SearchTimedOut,
)
from app.core.tools.tool_handler.search.scope import SearchScope
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.search_content_args import SearchContentArgs

SEARCH_CONTENT_DESCRIPTION = (
    "Search text inside one file or recursively inside a directory. The path can be a file "
    "or directory relative to the workspace. Use a regular expression in pattern; use "
    "file_glob to restrict directory searches to filenames such as '*.py'. Returns matching "
    "lines with file paths and line numbers. Use read_file when you already know the file "
    "and need its full contents."
)


class SearchContentTool(HandlerBase):
    """只读搜索单文件或递归目录内容的模型工具。"""

    name = "search_content"
    description = SEARCH_CONTENT_DESCRIPTION
    permission = "file_search"
    args_model = SearchContentArgs
    timeout_seconds = 30.0
    risk_level = "low"

    def execute(
        self,
        pattern: str,
        path: str,
        execution_context: ToolExecutionContext,
        file_glob: str | None = None,
        limit: int = 50,
        offset: int = 0,
        context: int = 0,
    ) -> ToolObservation:
        """解析文件/目录 scope，执行内容搜索并收口为 ToolObservation。"""

        workspace_root = execution_context.workspace_root
        resolver = PathResolver(workspace_root)
        device_error = resolver.blocked_device_reason(path)
        if device_error:
            return tool_error(
                self.name,
                device_error,
                reason=blocked_device_reason("searched"),
                permission=self.permission,
                status_hint="搜索失败",
            )
        resolved_path, path_error = resolver.resolve_without_boundary(path)
        if resolved_path is None:
            return _path_error(self.name, self.permission, path_error or path)
        try:
            scope = SearchScope.from_path(workspace_root, resolved_path)
            device_error = resolver.blocked_recursive_search_reason(path, resolved_path)
            if device_error:
                return tool_error(
                    self.name,
                    device_error,
                    reason=blocked_device_reason("searched"),
                    permission=self.permission,
                )
            page = search_content(
                scope,
                pattern,
                file_glob=file_glob,
                context=context,
                limit=limit,
                offset=offset,
                deadline=time.monotonic() + self.timeout_seconds,
            )
        except SearchTimedOut:
            return tool_error(
                self.name,
                "content search exceeded its time limit",
                reason="narrow the path or file_glob and call search_content again.",
                retryable=True,
                permission=self.permission,
                status_hint="搜索超时",
            )
        except InvalidSearchPattern as exc:
            return tool_error(
                self.name,
                f"invalid regular expression: {exc}",
                reason="correct pattern and call search_content again.",
                retryable=True,
                permission=self.permission,
                status_hint="正则无效",
            )
        except SearchPathNotFound as exc:
            return _path_error(self.name, self.permission, str(exc))
        except SearchPathUnreadable as exc:
            return tool_error(
                self.name,
                f"could not search path: {exc}",
                reason="provide a readable file or directory path.",
                retryable=True,
                permission=self.permission,
            )

        content = _format_content(page.matches)
        if page.total_rows > offset + limit:
            content += (
                f"\n\n[Hint: Results truncated ({page.total_rows} total lines). "
                f"Use offset={offset + limit} to see more, or narrow the pattern or path.]"
            )
        if not content:
            content = "No matches found."
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=content,
            display_data=build_content_search_display_data(
                pattern=pattern,
                path=path,
                matches=page.matches,
                offset=offset,
                limit=limit,
                total_rows=page.total_rows,
                match_count=page.match_count,
                scanned_files=page.scanned_files,
                skipped_files=page.skipped_files,
            ),
        )

    def to_definition(self) -> ToolDefinition:
        """返回注册用的 search_content 定义；执行保持在线程内。"""

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
                verb="搜索内容",
                icon="search",
                expandable=True,
                expand_layout="list",
                show_result=False,
            ),
        )


def build_search_content_definition() -> ToolDefinition:
    """构造 search_content 的注册定义。"""

    return SearchContentTool().to_definition_if_avaliable()


def _format_content(matches: tuple[Any, ...]) -> str:
    """把结构化行结果格式化成模型可读正文。"""

    return "\n".join(
        f"{item.file_path}:{item.line_number}:{'>' if item.is_match else ' '}{item.content}"
        for item in matches
    )


def _path_error(tool_name: str, permission: str, detail: str) -> ToolObservation:
    """构造统一的搜索路径错误。"""

    return tool_error(
        tool_name,
        f"could not search path: {detail}",
        reason="provide an existing file or directory path relative to the workspace.",
        retryable=True,
        permission=permission,
    )
