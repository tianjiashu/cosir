"""list_directory 工具实现。

本模块只承载 list_directory 这一个工具。列出项目内目录条目（名称 / 类型 /
大小 / mtime），路径安全委托 ``security.ProjectPathResolver``。

设计边界：
- 路径安全委托 ``security.ProjectPathResolver``，不内联路径规则。
- 只列目录，不读文件内容、不写文件。
"""

from datetime import UTC, datetime

from app.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.tools.tool_execute.tool_error import blocked_device_reason, tool_error
from app.tools.tool_handler.security.project_path import ProjectPathResolver
from app.tools.tool_models.list_directory_args import ListDirectoryArgs


class ListDirectoryTool:
    """列出项目内目录条目的工具类。

    参数:
        project_root: 允许列举的项目根目录。

    返回:
        ``ListDirectoryTool`` 实例。

    异常:
        初始化阶段不主动抛出业务异常。

    副作用:
        仅保存项目根与解析器；不读取、不写入文件。
    """

    name = "list_directory"
    description = (
        "List entries of a directory: name, type (file|dir), size, mtime. Read-only: "
        "relative paths resolve against the workspace root, and paths outside it are allowed."
    )
    permission = "file_search"
    args_model = ListDirectoryArgs
    timeout_seconds = 15.0
    risk_level = "low"

    def __init__(self) -> None:
        """初始化 list_directory 工具实例。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            仅保存 ``project_root``，不执行文件系统操作。
        """

    def execute(
        self,
        path: str,
        offset: int = 0,
        limit: int = 200,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """列出项目内目录条目，返回结构化观察结果。

        参数:
            path: 待列举的目录路径；相对路径以 workspace 根为基准，也接受项目根外的
                绝对路径（只读不受 workspace 边界限制）。
            offset: 跳过前 N 个排序后的条目。
            limit: 单页最多返回的条目数。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；由执行链
                在子进程内无条件注入的关键字参数，handler 契约必须接受此 kwarg 以匹配
                ``ToolExecutor._execute_handler`` 调用约定；本工具只读且不受 workspace
                边界限制，故不消费该值。

        返回:
            ``ToolObservation``；成功时 content 为紧凑的条目表，失败时 status 为
            error，``error``/``reason`` 提供面向模型的富文本诊断（``error``=发生了什么、
            ``reason``=为什么失败+如何修正+是否重试）。

        异常:
            不主动向上抛出；路径/读取错误归一化为结构化观察。

        副作用:
            只读目录结构，不修改文件系统。
        """
        root = execution_context.workspace_root
        resolver = ProjectPathResolver(root)
        device_error = resolver.blocked_device_reason(path)
        if device_error:
            return tool_error(
                self.name,
                device_error,
                reason=blocked_device_reason("listed"),
                permission=self.permission,
            )
        resolved, error = resolver.resolve_unrestricted(path)
        if resolved is None:
            return tool_error(
                self.name,
                f"could not list the directory: {error}",
                reason=(
                    "the directory path could not be resolved (common causes: empty value, "
                    "NUL characters, or a malformed path). Provide a valid, non-empty "
                    "directory path -- absolute, or relative to the project root -- and "
                    "retry; the same invalid value will always fail."
                ),
                permission=self.permission,
            )
        device_error = resolver.blocked_device_reason(path, resolved)
        if device_error:
            return tool_error(
                self.name,
                device_error,
                reason=blocked_device_reason("listed"),
                permission=self.permission,
            )
        if not resolved.exists():
            return tool_error(
                self.name,
                f"could not list the directory: no such path at '{resolved}'",
                reason=(
                    "the directory does not exist at the given path. Check for a typo, "
                    "confirm it was not moved or deleted, or pass an absolute path. The "
                    "same non-existent path will always fail, so retry only after the path "
                    "is corrected."
                ),
                permission=self.permission,
            )
        if not resolved.is_dir():
            return tool_error(
                self.name,
                f"could not list '{resolved}': it is a file, not a directory",
                reason=(
                    "the path resolves to a regular file; list_directory lists only "
                    "directories. Point to a directory, or use read_file to inspect a "
                    "file's contents; the same file path will always fail."
                ),
                permission=self.permission,
            )

        children = sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        page = children[offset : offset + limit]
        entries: list[str] = []
        for child in page:
            entry_type = "dir" if child.is_dir() else "file"
            try:
                stat = child.stat()
                size = stat.st_size if child.is_file() else 0
                modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
            except OSError:
                size = 0
                modified = "unknown"
            entries.append(f"{entry_type:4s} {size:>12}  {modified}  {child.name}")
        content = "\n".join(entries) if entries else "(empty directory)"
        next_offset = offset + len(page) if offset + len(page) < len(children) else None
        if next_offset is not None:
            content += (
                f"\n\n[Hint: Results truncated ({len(children)} total). "
                f"Use offset={next_offset} to continue.]"
            )
        return ToolObservation(
            tool_name=self.name,
            status="success",
            content=content,
            permission=self.permission,
            data={
                "total": len(children),
                "offset": offset,
                "limit": limit,
                "next_offset": next_offset,
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
                verb="列目录",
                icon="folder",
                summary_template="{path_basename}",
                detail_keys=("path", "offset", "limit"),
                click_action="open_file:{path}",
            ),
        )


def build_list_directory_definition() -> ToolDefinition:
    """构造绑定到指定项目根目录的 list_directory 工具定义。

    参数:
        无。

    返回:
        ``ToolDefinition``，供 ``ToolRegistry`` 注册。

    异常:
        无。

    副作用:
        创建 ``ListDirectoryTool`` 实例和定义对象，不执行文件系统操作。
    """

    return ListDirectoryTool().to_definition()
