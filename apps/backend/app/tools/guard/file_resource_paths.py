"""从工具调用参数推导文件状态与锁使用的资源路径。

本模块只做一件事：把「工具名 + 已校验参数 + 执行上下文」推导为一次文件工具调用
涉及的读取路径、写入路径、锁定路径与搜索范围（:class:`FileResourcePaths`）。

组织方式：
- :class:`FileResourceResolver` 把一次执行上下文对应的 workspace 根收进实例，其
  下的 ``resolve`` 与 ``_resolve_*`` 方法组织全部路径解析逻辑（消除反复传 ``root``）。
- 模块级 :func:`resolve_file_resource_paths` 是供调度链调用的薄入口：处理缺
  ``execution_context`` 的空资源兜底，并实例化解析器委托 :meth:`FileResourceResolver.resolve`。

明确不负责：
- 不遍历目录、不读取文件元数据（遍历与快照由 ``FileToolStateCoordinator`` 承担）。
- 不做重复调用签名归一（归一到 ``FileToolStateCoordinator``）。
- 不做路径空间运算（越界判定、祖先锁展开等归到 ``PathResolver`` 静态方法）。
- 不做设备/伪文件拦截本身（拦截由 ``PathResolver`` 承担，本模块只裁决结果）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.tools.schemas import ToolExecutionContext
from app.tools.tool_execute.tool_error import blocked_device_reason
from app.tools.tool_handler.patch import OperationType, parse_v4a_patch
from app.tools.tool_handler.security.path_resolver import PathResolver


class FileResourcePathError(ValueError):
    """文件资源路径在调度前被安全策略拒绝。"""

    def __init__(self, message: str, *, reason: str) -> None:
        """初始化带稳定 reason 的资源路径错误。

        参数:
            message: 人类可读错误说明（同时镜像进 ``ToolObservation.error``）。
            reason: ToolObservation 使用的面向模型富文本诊断（根因 + 修正建议 +
                与 ``retryable`` 一致的重试提示），而非机器短码。

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
    """一次工具调用涉及的读取、写入与搜索范围（纯解析结果，不含执行状态）。"""

    read_paths: tuple[Path, ...] = ()
    write_paths: tuple[Path, ...] = ()
    lock_paths: tuple[Path, ...] = ()
    scope_root: Path | None = None
    scope_recursive: bool = False
    scope_escapes_workspace: bool = False


class FileResourceResolver:
    """把一次工具调用的参数解析为文件资源路径（单一 workspace 根收进实例）。

    单一职责：按工具名把已校验参数解析为 :class:`FileResourcePaths`，路径解析
    以实例固化的 ``workspace_root`` 为基准（写/删强制 containment、只读越界放行）。
    所有 ``_resolve_*`` 方法共享同一个根，避免每次调用重复传 ``root`` 参数。

    参数:
        workspace_root: 本次工具调用所属 workspace 根目录。

    返回:
        ``FileResourceResolver`` 实例。

    异常:
        无。

    副作用:
        仅保存 ``workspace_root``，不读取、不写入文件。
    """

    def __init__(self, workspace_root: Path) -> None:
        """初始化解析器并固化 workspace 根。

        参数:
            workspace_root: 本次工具调用所属 workspace 根目录。

        返回:
            无。

        异常:
            无。

        副作用:
            仅保存 ``workspace_root``。
        """

        self._root = workspace_root

    def resolve(self, tool_name: str, arguments: Mapping[str, Any]) -> FileResourcePaths:
        """按工具名从已校验参数推导文件资源。

        参数:
            tool_name: 当前工具名称。
            arguments: 已通过 Pydantic 校验的工具参数。

        返回:
            用于 revision、重复调用检测和路径锁的资源路径；未知工具返回空资源。

        说明:
            对 ``patch``（replace 语义）与 ``apply_patch``（V4A 语义）两个工具，
            直接按 ``tool_name`` 分流，不再依赖 ``arguments["mode"]`` 入参（拆分后工具
            已无 ``mode`` 入参）；二者分别委托 :meth:`_patch_resources` 并传入
            ``is_v4a=False`` / ``is_v4a=True``。

        异常:
            无。无法解析的 patch 文本交由 handler 返回正式错误。

        副作用:
            无。
        """

        if tool_name == "read_file":
            target = self._resolve_read_path(arguments.get("path"), recursive=False)
            return FileResourcePaths(read_paths=(target,))
        if tool_name == "list_directory":
            scope = self._resolve_read_path(arguments.get("path"), recursive=True)
            return FileResourcePaths(
                scope_root=scope,
                scope_recursive=False,
            )
        if tool_name == "search_files":
            scope = self._resolve_read_path(arguments.get("path"), recursive=True)
            # 越界只读根（如 C:/Windows/System32）允许读取但禁止 prepare 阶段全量遍历，
            # 避免模型输入触发目录遍历 DoS；重复调用检测仅对 workspace 内 scope 生效。
            return FileResourcePaths(
                scope_root=scope,
                scope_recursive=True,
                scope_escapes_workspace=PathResolver.escapes_workspace(self._root, scope),
            )
        if tool_name == "write_file":
            target = self._resolve_containment_path(arguments.get("path"))
            return FileResourcePaths(
                write_paths=(target,),
                lock_paths=PathResolver.with_workspace_ancestors(self._root, (target,)),
            )
        if tool_name == "delete":
            # 与 DeleteTool.execute 首要解析（resolver.resolve_entry）同源：不跟随末级符号
            # 链接，使 prepare 的锁/写键与 handler 实际删除对象（链接自身或文件）一致，
            # 避免"删链接自身 vs 删链接目标"的键分歧；越界路径在调度前即被拒绝。
            target = self._resolve_workspace_path(
                arguments.get("path"),
                resolve_entry=True,
                action="deleted",
            )
            return FileResourcePaths(
                write_paths=(target,),
                lock_paths=PathResolver.with_workspace_ancestors(self._root, (target,)),
            )
        if tool_name == "patch":
            # patch 工具固定 replace 语义（原 mode=="replace" 分支）。
            resources = self._patch_resources(arguments, is_v4a=False)
            return FileResourcePaths(
                write_paths=resources.write_paths,
                lock_paths=PathResolver.with_workspace_ancestors(self._root, resources.write_paths),
            )
        if tool_name == "apply_patch":
            # apply_patch 工具固定 V4A 语义（原 mode=="patch" 分支）。
            resources = self._patch_resources(arguments, is_v4a=True)
            return FileResourcePaths(
                write_paths=resources.write_paths,
                lock_paths=PathResolver.with_workspace_ancestors(self._root, resources.write_paths),
            )
        return FileResourcePaths()

    def _patch_resources(self, arguments: Mapping[str, Any], *, is_v4a: bool) -> FileResourcePaths:
        """推导 replace / V4A patch 涉及的写路径。

        参数:
            arguments: 已校验 patch 参数。
            is_v4a: ``False`` 时为 replace 语义（``patch`` 工具，原 ``mode=="replace"``
                分支），直接按 ``path`` 推导单文件写路径；``True`` 时为 V4A 语义
                （``apply_patch`` 工具，原 ``mode=="patch"`` 分支），解析 V4A 文本后
                提取多个文件写路径。

        返回:
            patch 涉及的去重写路径；无有效路径时返回空资源。

        异常:
            :class:`FileResourcePathError`：写路径解析为空、含 NUL 或越界 workspace
            时经 :func:`_resolve_containment_path` 抛出。无法解析的 V4A 文本**不**抛
            出异常，仅返回空资源交由 handler 返回正式错误。

        副作用:
            无。
        """

        if not is_v4a:
            # replace 语义（patch 工具）：直接按 path 推导单文件写路径，无需解析 V4A。
            path = arguments.get("path")
            if isinstance(path, str) and path:
                return FileResourcePaths(write_paths=(self._resolve_containment_path(path),))
            return FileResourcePaths()

        # V4A 语义（apply_patch 工具）：解析 V4A 文本后提取多文件写路径。
        patch_text = arguments.get("patch")
        if not isinstance(patch_text, str):
            return FileResourcePaths()
        operations, parse_error = parse_v4a_patch(patch_text)
        if parse_error:
            return FileResourcePaths()

        paths: list[Path] = []
        for operation in operations:
            paths.append(self._resolve_containment_path(operation.file_path))
            if operation.operation == OperationType.MOVE and operation.new_path:
                paths.append(self._resolve_containment_path(operation.new_path))
        return FileResourcePaths(write_paths=tuple(dict.fromkeys(paths)))

    def _resolve_workspace_path(
        self,
        value: Any,
        *,
        resolve_entry: bool,
        action: str,
    ) -> Path:
        """把写类/删类工具路径参数解析到 workspace 内绝对路径，越界即拒绝。

        空串 / NUL / 越界三类校验与 ``PathResolver._validate_path`` 语义对齐，仅在此
        定制富文本 ``reason``。``resolve_entry`` 控制末级符号链接是否跟随：``False`` 时
        调用 ``resolver.resolve_within_workspace``（write/patch handler 同源，跟随链接），
        ``True`` 时调用 ``resolver.resolve_entry_within_workspace``（delete handler 首位解析
        同源，不跟随末级链接，使 prepare 的锁/写键与"删链接自身"一致）。越界路径在调度前
        即被拦截，不再泄漏哨兵、不纳入锁。

        参数:
            value: 工具 path 参数；空串 / ``None`` / 非字符串直接拒绝。
            resolve_entry: 是否不跟随末级符号链接（delete 为 True）。
            action: 受影响的动作英文动名词（"written" / "deleted"），用于定制越界 reason。

        返回:
            workspace 内规范绝对路径；``resolve_entry=True`` 时末级保持词法名称。

        异常:
            FileResourcePathError: 路径为空、含 NUL、或解析到 workspace 外时抛出；
                ``reason`` 为面向模型的富文本诊断。

        副作用:
            无。
        """

        if not isinstance(value, str) or not value.strip():
            raise FileResourcePathError(
                "path must be a non-empty string",
                reason=(
                    "the path argument is missing or empty; pass a file or directory path "
                    "relative to the workspace root (or an absolute path under it). The same "
                    "empty path will always be rejected."
                ),
            )
        if "\x00" in value:
            raise FileResourcePathError(
                "path must not contain NUL characters",
                reason="the path contains a NUL character; pass a valid path string.",
            )
        resolver = PathResolver(self._root)
        if resolve_entry:
            resolved, error = resolver.resolve_entry_within_workspace(value)
        else:
            resolved, error = resolver.resolve_within_workspace(value)
        if resolved is None:
            raise FileResourcePathError(
                error or "path escapes the project root",
                reason=(
                    f"the path resolves outside the workspace root and cannot be {action} "
                    f"for safety. The resolver rejects paths that point outside the workspace; "
                    f"pass a path inside the project (relative to the workspace root, or an "
                    f"absolute path under it), and the same out-of-bounds path will always be "
                    f"rejected."
                ),
            )
        return resolved

    def _resolve_containment_path(self, value: Any) -> Path:
        """把写类工具路径解析到 workspace 内绝对路径（跟随末级符号链接，与 handler 同源）。

        参数:
            value: 工具 path 参数。

        返回:
            workspace 内规范绝对路径。

        异常:
            FileResourcePathError: 路径为空、含 NUL、或解析到 workspace 外时抛出。

        副作用:
            无。
        """

        return self._resolve_workspace_path(value, resolve_entry=False, action="written or deleted")

    def _resolve_read_path(
        self,
        value: Any,
        *,
        recursive: bool,
    ) -> Path:
        """在任何 snapshot 遍历前解析并裁决只读资源路径。

        参数:
            value: read/list/search 的路径参数。
            recursive: 是否按递归搜索根规则阻止 proc/sys/dev 树。

        返回:
            unrestricted 解析后的绝对路径。

        异常:
            FileResourcePathError: 输入非法、无法解析或命中设备/伪文件规则；
                ``reason`` 为面向模型的富文本诊断。

        副作用:
            仅读取路径元数据，不遍历目录。
        """

        # 模型输入的路径
        raw = value if isinstance(value, str) and value else "."
        resolver = PathResolver(self._root)

        # 第一层设备安检（看原始输入字符串）
        initial_reason = (
            resolver.blocked_recursive_search_reason(raw)
            if recursive
            else resolver.blocked_device_reason(raw)
        )
        if initial_reason:
            raise FileResourcePathError(
                initial_reason,
                reason=blocked_device_reason("read" if not recursive else "searched recursively"),
            )

        # 核心：解析成绝对路径（不限制 workspace 边界，供只读工具越界读取）
        resolved, error = resolver.resolve_without_boundary(raw)
        if resolved is None:
            raise FileResourcePathError(
                error or "cannot resolve path",
                reason=(
                    "the path cannot be resolved to a regular file or directory; pass a "
                    "well-formed path inside the project. The same malformed path will "
                    "always be rejected."
                ),
            )

        # 第二层设备安检（看符号链接解开的真实目标）
        resolved_reason = (
            resolver.blocked_recursive_search_reason(raw, resolved)
            if recursive
            else resolver.blocked_device_reason(raw, resolved)
        )
        if resolved_reason:
            raise FileResourcePathError(
                resolved_reason,
                reason=blocked_device_reason("read" if not recursive else "searched recursively"),
            )
        return resolved


def resolve_file_resource_paths(
    tool_name: str,
    arguments: Mapping[str, Any],
    execution_context: ToolExecutionContext | None,
) -> FileResourcePaths:
    """从工具名和已校验参数推导文件资源（供调度链调用的薄入口）。

    处理缺 ``execution_context`` 的空资源兜底，并实例化 :class:`FileResourceResolver`
    委托 :meth:`FileResourceResolver.resolve`；保持原调用点签名不变。

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
    return FileResourceResolver(execution_context.workspace_root).resolve(tool_name, arguments)
