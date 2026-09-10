"""search_files 工具实现（合并原 grep 与 search_files）。

本模块只承载 search_files 这一个工具，按 ``target`` 分流为 content（内容正则搜索，
原 grep）与 files（文件名 glob 查找并按 mtime 降序）两种能力，对齐 Hermes
``search_files`` 的参数面与输出语义（file_glob / limit / offset /
output_mode=files_only / context / 分页续读提示）。

设计边界：
- 路径安全仅约束搜索根（项目根 + 可选子目录），不做写操作。
- 不内联遍历/匹配逻辑，复用 ``search`` 包引擎。
- 成功/失败观察统一经 ``tool_execute.tool_success`` / ``tool_error`` 工厂构造。
"""

from typing import Any

from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import blocked_device_reason, tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.search import search_content, search_filenames
from app.core.tools.tool_handler.search.error_prefixes import (
    INVALID_REGEX_PREFIX,
    PATH_NOT_FOUND_PREFIX,
)
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.search_files_args import SearchFilesArgs

SEARCH_FILES_DESCRIPTION = (
    "Search file contents or find files by name. Use this instead of grep/rg/find/ls "
    "in terminal.\n\n"
    "Content search (target='content'): Regex search inside files. Output modes: full "
    "matches with line numbers, file paths only, or match counts.\n\n"
    "File search (target='files'): Find files by glob pattern (e.g., '*.py', "
    "'*config*'). Also use this instead of ls — results sorted by modification time."
)


class SearchFilesTool(HandlerBase):
    """在项目内搜索文件内容或按文件名查找文件的合并工具类。

    参数:
        project_root: 搜索根目录（项目根）。

    返回:
        ``SearchFilesTool`` 实例。

    异常:
        初始化阶段不主动抛出业务异常。

    副作用:
        仅保存项目根；不读取、不写入文件（搜索引擎内部只读）。
    """

    name = "search_files"
    description = SEARCH_FILES_DESCRIPTION
    permission = "file_search"
    args_model = SearchFilesArgs
    timeout_seconds = 30.0
    risk_level = "low"

    def __init__(self) -> None:
        """初始化 search_files 工具实例。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        """

    def execute(
        self,
        pattern: str,
        execution_context: ToolExecutionContext,
        target: str = "content",
        path: str | None = None,
        file_glob: str | None = None,
        limit: int = 50,
        offset: int = 0,
        output_mode: str = "content",
        context: int = 0,
    ) -> ToolObservation:
        """按 target 分流执行内容搜索或文件名查找，统一收口成功/失败观察。

        参数:
            pattern: content 模式为正则表达式；files 模式为 glob 模式。
            target: ``content``（搜内容，默认）或 ``files``（按名查文件）。
            path: 可选的基于项目根的子目录（仅在该范围内搜索）。
            file_glob: content 模式下的文件名 glob 过滤。
            limit: 单页最大结果条数。
            offset: 跳过前 N 条结果用于分页。
            output_mode: content 模式输出格式 content / files_only / count。
            context: content 模式命中行前后的上下文行数。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；由执行链
                在子进程内无条件注入的关键字参数，handler 契约必须接受此 kwarg 以匹配
                ``ToolHandlerRunner._execute_handler`` 调用约定；本工具只读且不受 workspace
                边界限制，故不消费该值。

        返回:
            ``ToolObservation``；成功时 content 为格式化搜索结果（无命中时为提示、
            分页截断时含 offset 续读提示），失败时 status 为 error，``error``/``reason``
            提供面向模型的富文本诊断（``error``=发生了什么、``reason``=为什么失败+
            如何修正+是否重试）。

        异常:
            不主动向上抛出；搜索错误归一化为结构化观察。

        副作用:
            只读文件系统（搜索引擎内部）。
        """
        workspace_root = execution_context.workspace_root
        resolver = PathResolver(workspace_root)
        search_path = path or "."
        device_error = resolver.blocked_recursive_search_reason(search_path)
        if device_error:
            return tool_error(
                self.name,
                device_error,
                reason=blocked_device_reason("searched recursively"),
                permission=self.permission,
            )
        resolved_path, path_error = resolver.resolve_without_boundary(search_path)
        if resolved_path is None:
            return tool_error(
                self.name,
                f"could not search: {path_error}",
                reason=(
                    "the search path could not be resolved to a readable directory "
                    "(common causes: empty value, NUL characters, or a malformed path). "
                    "Provide a valid, non-empty directory path -- absolute, or relative "
                    "to the project root -- and retry; the same invalid value will "
                    "always fail."
                ),
                permission=self.permission,
            )
        device_error = resolver.blocked_recursive_search_reason(search_path, resolved_path)
        if device_error:
            return tool_error(
                self.name,
                device_error,
                reason=blocked_device_reason("searched recursively"),
                permission=self.permission,
            )
        resolved_path_text = str(resolved_path)
        display_items: list[Any]
        if target == "files":
            result, match_count, file_items = search_filenames(
                workspace_root,
                pattern,
                path=resolved_path_text,
                limit=limit,
                offset=offset,
            )
            display_items = file_items
            empty_message = "No files found."
        else:
            result, match_count, hit_items = search_content(
                workspace_root,
                pattern,
                path=resolved_path_text,
                file_glob=file_glob,
                output_mode=output_mode,
                context=context,
                limit=limit,
                offset=offset,
            )
            display_items = hit_items
            empty_message = "No matches found."

        if result.startswith(INVALID_REGEX_PREFIX):
            return tool_error(
                self.name,
                result,
                reason=(
                    "the search pattern is not a valid regular expression, so the content "
                    "search could not run. Fix the pattern (escape literal special "
                    "characters such as . * + ? ( ) [ ] { } | ^ $ with a backslash, or "
                    "use a simpler substring) and retry; the same malformed pattern will "
                    "always fail."
                ),
                permission=self.permission,
            )
        if result.startswith(PATH_NOT_FOUND_PREFIX):
            return tool_error(
                self.name,
                result,
                reason=(
                    "the search path does not exist (no such directory), so nothing "
                    "could be searched. Check the path for a typo or confirm the "
                    "directory exists, and retry with a valid path; the same missing "
                    "path will always fail."
                ),
                permission=self.permission,
            )
        if not result:
            result = empty_message
        file_paths = _display_file_paths(display_items, target)
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=result,
            display_data={
                "kind": "file-list",
                "files": [{"path": file_path} for file_path in file_paths],
                "pattern": pattern,
                "target": target,
                "path": search_path,
                "page": {
                    "offset": offset,
                    "limit": limit,
                    "has_more": match_count > offset + limit,
                    "next_offset": offset + limit if match_count > offset + limit else None,
                },
            },
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
                verb="搜索文件",
                icon="search",
                expandable=True,
                expand_layout="list",
            ),
        )


def build_search_files_definition() -> ToolDefinition:
    """构造绑定到指定项目根目录的 search_files 工具定义。

    参数:
        无

    返回:
        ``ToolDefinition``，供 ``ToolRegistry`` 注册。

    异常:
        无。

    副作用:
        创建 ``SearchFilesTool`` 实例和定义对象，不执行文件系统操作。
    """

    return SearchFilesTool().to_definition()


def _display_file_paths(items: list[Any], target: str) -> list[str]:
    """把搜索引擎结果归一为 UI 只消费的去重文件路径列表。"""

    paths: list[str] = []
    seen: set[str] = set()
    for item in items:
        if target == "files":
            path = item
        elif isinstance(item, dict):
            path = item.get("file_path", "")
        else:
            path = ""
        if not isinstance(path, str) or not path or path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths
