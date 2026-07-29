"""从工具调用参数推导文件状态与锁使用的资源路径。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.tools.schemas import ToolExecutionContext
from app.tools.tool_handler.patch import OperationType, parse_v4a_patch
from app.tools.tool_handler.security.project_path import ProjectPathResolver


class FileResourcePathError(ValueError):
    """文件资源路径在调度前被安全策略拒绝。"""

    def __init__(self, message: str, *, reason: str) -> None:
        """初始化带稳定 reason 的资源路径错误。

        参数:
            message: 人类可读错误说明。
            reason: ToolObservation 使用的稳定失败分类。

        返回:
            无。

        异常:
            无。

        副作用:
            保存 ``reason``。
        """

        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class FileResourcePaths:
    """一次工具调用涉及的读取、写入与搜索范围。"""

    read_paths: tuple[Path, ...] = ()
    write_paths: tuple[Path, ...] = ()
    lock_paths: tuple[Path, ...] = ()
    scope_root: Path | None = None
    scope_recursive: bool = False


def resolve_file_resource_paths(
    tool_name: str,
    arguments: Mapping[str, Any],
    execution_context: ToolExecutionContext | None,
) -> FileResourcePaths:
    """从工具名和已校验参数推导文件资源。

    参数:
        tool_name: 当前工具名称。
        arguments: 已通过 Pydantic 校验的工具参数。
        execution_context: 当前 task/workspace 执行上下文。

    返回:
        用于 revision、重复调用检测和路径锁的资源路径；缺 context 时返回空资源。

    异常:
        无。无法解析的 patch 文本交由 handler 返回正式错误。

    副作用:
        无。
    """

    if execution_context is None:
        return FileResourcePaths()
    root = execution_context.workspace_root

    if tool_name == "read_file":
        target = _resolve_unrestricted_resource(root, arguments.get("path"), recursive=False)
        return FileResourcePaths(read_paths=(target,))
    if tool_name == "list_directory":
        scope = _resolve_unrestricted_resource(root, arguments.get("path"), recursive=True)
        return FileResourcePaths(
            scope_root=scope,
            scope_recursive=False,
        )
    if tool_name == "search_files":
        scope = _resolve_unrestricted_resource(root, arguments.get("path"), recursive=True)
        return FileResourcePaths(
            scope_root=scope,
            scope_recursive=True,
        )
    if tool_name == "write_file":
        target = _resolve_argument_path(root, arguments.get("path"))
        return FileResourcePaths(
            write_paths=(target,),
            lock_paths=_with_workspace_ancestors(root, (target,)),
        )
    if tool_name == "delete":
        target = _resolve_entry_argument_path(root, arguments.get("path"))
        return FileResourcePaths(
            write_paths=(target,),
            lock_paths=_with_workspace_ancestors(root, (target,)),
        )
    if tool_name == "patch":
        resources = _patch_resources(root, arguments)
        return FileResourcePaths(
            write_paths=resources.write_paths,
            lock_paths=_with_workspace_ancestors(root, resources.write_paths),
        )
    return FileResourcePaths()


def _patch_resources(root: Path, arguments: Mapping[str, Any]) -> FileResourcePaths:
    """推导 replace/V4A patch 涉及的写路径。

    参数:
        root: 当前 workspace 根目录。
        arguments: 已校验 patch 参数。

    返回:
        patch 涉及的去重写路径。

    异常:
        无。

    副作用:
        无。
    """

    if arguments.get("mode", "replace") == "replace":
        path = arguments.get("path")
        if isinstance(path, str) and path:
            return FileResourcePaths(write_paths=(_resolve_argument_path(root, path),))
        return FileResourcePaths()

    patch_text = arguments.get("patch")
    if not isinstance(patch_text, str):
        return FileResourcePaths()
    operations, parse_error = parse_v4a_patch(patch_text)
    if parse_error:
        return FileResourcePaths()

    paths: list[Path] = []
    for operation in operations:
        paths.append(_resolve_argument_path(root, operation.file_path))
        if operation.operation == OperationType.MOVE and operation.new_path:
            paths.append(_resolve_argument_path(root, operation.new_path))
    return FileResourcePaths(write_paths=tuple(dict.fromkeys(paths)))


def _resolve_argument_path(root: Path, value: Any) -> Path:
    """把工具路径参数转换成不要求存在的绝对路径。

    参数:
        root: 相对路径解析基准。
        value: 工具参数值；非字符串或空值代表根目录。

    返回:
        绝对路径；最终 containment 仍由 handler 的 ``ProjectPathResolver`` 裁决。

    异常:
        无。

    副作用:
        无。
    """

    if isinstance(value, str) and "\x00" in value:
        raise ValueError("path must not contain NUL characters")
    raw = Path(value) if isinstance(value, str) and value else Path("../tool_execute")
    return (raw if raw.is_absolute() else root / raw).resolve(strict=False)


def _resolve_unrestricted_resource(
    root: Path,
    value: Any,
    *,
    recursive: bool,
) -> Path:
    """在任何 snapshot 遍历前解析并裁决只读资源路径。

    参数:
        root: 相对路径解析基准。
        value: read/list/search 的路径参数。
        recursive: 是否按递归搜索根规则阻止 proc/sys/dev 树。

    返回:
        unrestricted 解析后的绝对路径。

    异常:
        FileResourcePathError: 输入非法、无法解析或命中设备/伪文件规则。

    副作用:
        仅读取路径元数据，不遍历目录。
    """

    raw = value if isinstance(value, str) and value else "."
    resolver = ProjectPathResolver(root)
    initial_reason = (
        resolver.blocked_recursive_search_reason(raw)
        if recursive
        else resolver.blocked_device_reason(raw)
    )
    if initial_reason:
        raise FileResourcePathError(initial_reason, reason="blocked_device")
    resolved, error = resolver.resolve_unrestricted(raw)
    if resolved is None:
        raise FileResourcePathError(error, reason="invalid_path")
    resolved_reason = (
        resolver.blocked_recursive_search_reason(raw, resolved)
        if recursive
        else resolver.blocked_device_reason(raw, resolved)
    )
    if resolved_reason:
        raise FileResourcePathError(resolved_reason, reason="blocked_device")
    return resolved


def _resolve_entry_argument_path(root: Path, value: Any) -> Path:
    """解析目录项自身而不跟随最后一级符号链接。

    参数:
        root: 相对路径解析基准。
        value: delete 工具的路径参数。

    返回:
        父目录已解析、最终目录项保持词法名称的绝对路径。

    异常:
        无。

    副作用:
        无。
    """

    if isinstance(value, str) and "\x00" in value:
        raise ValueError("path must not contain NUL characters")
    raw = Path(value) if isinstance(value, str) and value else Path("../tool_execute")
    target = raw if raw.is_absolute() else root / raw
    return target.parent.resolve(strict=False) / target.name


def _with_workspace_ancestors(root: Path, paths: tuple[Path, ...]) -> tuple[Path, ...]:
    """为写路径补充 workspace 根以下的祖先目录锁。

    参数:
        root: 当前 workspace 根。
        paths: handler 实际涉及的文件或目录路径。

    返回:
        去重后的路径及祖先目录；workspace 根自身不纳入，避免无关顶层目录全串行。

    异常:
        无。越界路径仅保留自身，最终仍由 handler containment 拒绝。

    副作用:
        无。
    """

    workspace_root = root.resolve(strict=False)
    expanded: list[Path] = []
    for path in paths:
        expanded.append(path)
        current = path.parent
        while current != workspace_root:
            try:
                current.relative_to(workspace_root)
            except ValueError:
                break
            expanded.append(current)
            current = current.parent
    return tuple(dict.fromkeys(expanded))
