"""list_directory 工具实现。

本模块只承载 list_directory 这一个工具。列出项目内目录条目（名称 / 类型 /
大小 / mtime），路径安全委托 ``security.ProjectPathResolver``。

设计边界：
- 路径安全委托 ``security.ProjectPathResolver``，不内联路径规则。
- 只列目录，不读文件内容、不写文件。
"""

import fnmatch
import os
from datetime import UTC, datetime
from typing import Any

from app.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.tools.tool_execute.tool_error import blocked_device_reason, tool_error
from app.tools.tool_execute.tool_success import tool_success
from app.tools.tool_handler.security.project_path import ProjectPathResolver
from app.tools.tool_handler.tool_base import HandlerBase
from app.tools.tool_models.list_directory_args import ListDirectoryArgs


class ListDirectoryTool(HandlerBase):
    """列出项目内目录条目的工具类（无状态）。

    返回:
        ``ListDirectoryTool`` 实例。

    异常:
        初始化阶段不主动抛出业务异常。

    副作用:
        不持有文件系统状态，不读取、不写入文件；项目根在执行时从
        ``execution_context.workspace_root`` 取得。
    """

    name = "list_directory"
    description = (
        "List entries of a directory: name, type (file|dir), size, mtime. Read-only: "
        "relative paths resolve against the workspace root, and paths outside it are allowed. "
        "Hidden (dot) entries are skipped unless include_hidden is true; entries whose name "
        "matches any ignore_globs pattern are also excluded."
    )
    permission = "file_search"
    args_model = ListDirectoryArgs
    timeout_seconds = 15.0
    risk_level = "low"

    def __init__(self) -> None:
        """初始化 list_directory 工具实例（无状态）。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            不保存任何状态、不执行文件系统操作；项目根在执行时从
            ``execution_context.workspace_root`` 取得。
        """

    def execute(
        self,
        path: str,
        offset: int = 0,
        limit: int = 200,
        include_hidden: bool = False,
        ignore_globs: list[str] | None = None,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """列出项目内目录条目，返回结构化观察结果。

        参数:
            path: 待列举的目录路径；相对路径以 workspace 根为基准，也接受项目根外的
                绝对路径（只读不受 workspace 边界限制）。
            offset: 跳过前 N 个排序后的条目。
            limit: 单页最多返回的条目数。
            include_hidden: 为 true 时一并列出 dot 条目（名称以 ``.`` 开头），默认跳过
                以保持目录清单紧凑。
            ignore_globs: 匹配条目名即排除的 glob 模式列表；``None``/空列表表示不排除。
                与 ``include_hidden`` 独立叠加（先按可见性过滤，再按本参数排除）。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；
                由执行链在执行期强制注入，handler 契约必须接受此 kwarg。
                本工具只读，但解析相对路径仍需工作区根，故消费其 ``workspace_root``。

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

        with os.scandir(resolved) as scan:
            raw_entries = [
                entry for entry in scan if include_hidden or not entry.name.startswith(".")
            ]
            if ignore_globs:
                raw_entries = [
                    entry
                    for entry in raw_entries
                    if not any(fnmatch.fnmatch(entry.name, g) for g in ignore_globs)
                ]
            children = sorted(raw_entries, key=lambda e: (not e.is_dir(), e.name.lower()))
            page = children[offset : offset + limit]
            entries: list[str] = []
            entry_dicts: list[dict[str, str]] = []
            for entry in page:
                entry_type = "dir" if entry.is_dir() else "file"
                try:
                    stat = entry.stat()
                    size = stat.st_size if entry.is_file() else 0
                    modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
                except OSError:
                    size = 0
                    modified = "unknown"
                entries.append(f"{entry_type:4s} {size:>12}  {modified}  {entry.name}")
                entry_dicts.append({"name": entry.name, "type": entry_type, "path": path})
        content = "\n".join(entries) if entries else "(empty directory)"
        next_offset = offset + len(page) if offset + len(page) < len(children) else None
        if next_offset is not None:
            content += (
                f"\n\n[Hint: Results truncated ({len(children)} total). "
                f"Use offset={next_offset} to continue.]"
            )

        return tool_success(
            tool_name=self.name,
            content=content,
            permission=self.permission,
        )

    def render_request_summary(self, arguments: dict[str, Any]) -> str:
        """
        返回 list_directory 请求摘要。TODO: 待实现
        """

    def render_result_summary(self, display_data: dict[str, Any]) -> str | None:
        """
        返回 list_directory 展开态摘要。TODO: 待实现
        """

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
                verb="读取",
                icon="folder",
                title_summary=self.render_request_summary,
                result_summary=self.render_result_summary,
                expandable=True,
                expand_layout="list",
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
