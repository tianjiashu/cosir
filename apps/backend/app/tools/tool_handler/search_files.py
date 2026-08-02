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

from pathlib import Path
from typing import Any

from app.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.tools.tool_execute.tool_error import blocked_device_reason, tool_error
from app.tools.tool_execute.tool_success import tool_success
from app.tools.tool_handler.search import search_content, search_filenames
from app.tools.tool_handler.search.error_prefixes import (
    INVALID_REGEX_PREFIX,
    PATH_NOT_FOUND_PREFIX,
)
from app.tools.tool_handler.security.project_path import ProjectPathResolver
from app.tools.tool_handler.tool_base import HandlerBase
from app.tools.tool_models.search_files_args import SearchFilesArgs

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
        target: str = "content",
        path: str | None = None,
        file_glob: str | None = None,
        limit: int = 50,
        offset: int = 0,
        output_mode: str = "content",
        context: int = 0,
        execution_context: ToolExecutionContext = None,
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
                ``ToolExecutor._execute_handler`` 调用约定；本工具只读且不受 workspace
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
        resolver = ProjectPathResolver(workspace_root)
        search_path = path or "."
        device_error = resolver.blocked_recursive_search_reason(search_path)
        if device_error:
            return tool_error(
                self.name,
                device_error,
                reason=blocked_device_reason("searched recursively"),
                permission=self.permission,
            )
        resolved_path, path_error = resolver.resolve_unrestricted(search_path)
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
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=result,
            display_data={
                "items": display_items,
                "pattern": pattern,
                "target": target,
                "path": search_path,
            },
        )

    def render_request_summary(self, arguments: dict[str, Any]) -> str:
        """返回 search_files 执行前摘要。

        参数:
            arguments: 工具调用参数字典。

        返回:
            搜索模式摘要；尽量贴近前端折叠行展示。

        异常:
            无。

        副作用:
            无。
        """
        pattern = str(arguments.get("pattern") or "")
        target = str(arguments.get("target") or "content")
        path = str(arguments.get("path") or ".")
        file_glob = arguments.get("file_glob")
        if target == "files":
            return f"{pattern} in {path}" if path != "." else pattern
        if file_glob:
            return f"{pattern} in {file_glob}"
        return f"{pattern} in {path}" if path != "." else pattern

    def render_result_summary(
        self,
        display_data: dict[str, Any],
    ) -> str | list[dict[str, Any]] | None:
        """把 search_files 执行元数据投影为前端搜索结果列表。

        参数:
            display_data: 工具观察里的客户端展示数据。

        返回:
            失败时返回错误摘要；无命中时返回空状态文案；有命中时返回列表条目。

        异常:
            无。

        副作用:
            无。
        """
        if display_data.get("status") == "error":
            return "error:" + str(display_data.get("error", ""))
        target = str(display_data.get("target") or "content")
        search_path = str(display_data.get("path") or ".")
        raw_items = display_data.get("items", [])
        if not isinstance(raw_items, list) or not raw_items:
            return "没有搜索到相关内容"
        if target == "files" and isinstance(raw_items, list):
            return self._filename_entries(
                [str(item) for item in raw_items],
                search_path,
            )
        return self._content_entries(
            [item for item in raw_items if isinstance(item, dict)],
            search_path,
        )

    def _content_entries(
        self,
        items: list[dict[str, Any]],
        search_path: str,
    ) -> list[dict[str, Any]]:
        """把内容搜索命中转换成前端 list 布局条目。

        参数:
            items: 搜索引擎返回的命中行结构。
            search_path: 用户指定的搜索根。

        返回:
            前端可按字段形状渲染的搜索命中列表。

        异常:
            无。

        副作用:
            无。
        """
        return [self._content_entry(item, search_path) for item in items]

    def _content_entry(self, item: dict[str, Any], search_path: str) -> dict[str, Any]:
        """把单条内容命中转换为前端搜索结果行。

        参数:
            item: 搜索引擎返回的单条命中。
            search_path: 用户指定的搜索根。

        返回:
            前端必要的搜索结果行数据。

        异常:
            无。

        副作用:
            无。
        """

        file_path = self._join_display_path(search_path, str(item.get("file_path", "")))
        name, parent = self._split_display_path(file_path)
        line_number = int(item.get("line_number", 0) or 0)
        return {
            "kind": "content_match",
            "icon": "file",
            "name": name,
            "path": parent,
            "line_number": line_number,
            "line_label": f"#L{line_number}" if line_number > 0 else "",
        }

    def _filename_entries(self, items: list[str], search_path: str) -> list[dict[str, Any]]:
        """把文件名搜索结果转换成前端 list 布局条目。

        参数:
            items: 搜索引擎返回的相对路径列表。
            search_path: 用户指定的搜索根。

        返回:
            前端可按 ``name`` / ``path`` / ``type`` 渲染的文件条目列表。

        异常:
            无。

        副作用:
            无。
        """
        entries: list[dict[str, Any]] = []
        for item in items:
            item_path = Path(item)
            parent = item_path.parent.as_posix()
            parent = search_path if parent == "." else self._join_display_path(search_path, parent)
            entries.append(
                {
                    "kind": "file_result",
                    "icon": "file",
                    "name": item_path.name,
                    "path": parent,
                    "type": "file",
                }
            )
        return entries

    def _join_display_path(self, search_path: str, relative_path: str) -> str:
        """拼接搜索根与相对结果路径，仅用于展示。

        参数:
            search_path: 用户指定的搜索根。
            relative_path: 搜索引擎返回的相对路径。

        返回:
            POSIX 风格展示路径；绝对搜索根或 ``.`` 场景保持可读降级。

        异常:
            无。

        副作用:
            无。
        """
        if not relative_path:
            return search_path or "."
        if not search_path or search_path == ".":
            return Path(relative_path).as_posix()
        if Path(search_path).is_absolute():
            return Path(relative_path).as_posix()
        return (Path(search_path) / relative_path).as_posix()

    def _split_display_path(self, file_path: str) -> tuple[str, str]:
        """拆分前端展示用文件名与父路径。

        参数:
            file_path: POSIX 风格文件路径。

        返回:
            ``(name, parent_path)``。

        异常:
            无。

        副作用:
            无。
        """

        path = Path(file_path)
        parent = path.parent.as_posix()
        return path.name, "" if parent == "." else parent

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
                # 折叠态与结果摘要同形：渲染入口已补齐 path/file_glob 派生字段。
                title_summary=self.render_request_summary,
                result_summary=self.render_result_summary,
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
