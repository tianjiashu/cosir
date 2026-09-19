"""根据执行前捕获的工作区状态校验结构化文件变更。"""

from __future__ import annotations

import errno
import hashlib
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

from app.core.tools.guard.file_mutation_state import (
    directory_identity,
    is_relative_ancestor,
    state_matches,
)

CaptureStates = Callable[[Path, Sequence[str]], dict[str, dict[str, Any]]]


class FileMutationGuard:
    """在文件工具执行同步变更前校验工作区状态。"""

    def __init__(
        self,
        payload: dict[str, Any],
        root: Path,
        *,
        capture_states: CaptureStates,
        trash_root: Path | None = None,
    ) -> None:
        """将预备快照和本次处理器调用所需的回调绑定起来。

        参数:
            payload: 快照当前载荷；原地更新 ``paths_after`` 和目录身份。
            root: 工作区的规范根路径。
            capture_states: 捕获给定相对路径当前状态的只读回调。
            trash_root: 删除操作的专用暂存根目录；仅当本操作需要把删除目标移入
                trash 时传入，否则为 ``None``（此时删除仍走即时 unlink 兜底）。

        返回:
            无。

        异常:
            无。

        副作用:
            保存本次操作使用的回调和内存状态；不读写文件或数据库。
        """
        self._payload = payload
        self._root = root
        self._capture_states = capture_states
        self._trash_root = trash_root

    def before_write(self, path: Path, content: bytes) -> None:
        """校验并记录目标文件的准确字节内容及隐式创建的父目录。

        参数:
            path: 处理器要写入的目标路径，必须已包含在此快照中。
            content: 处理器即将写入的准确字节内容。

        返回:
            无。

        异常:
            OSError: 路径越出工作区。
            ValueError: 路径不在预备快照中。
            RuntimeError: 当前路径状态与预期状态不再匹配。

        副作用:
            读取工作区元数据，并更新内存中的预期状态。
        """
        relative = self._relative(path)
        expected_after: dict[str, dict[str, Any]] = {}
        before = self._payload["paths_before"]
        parent = Path(os.path.abspath(path)).parent
        while parent != self._root:
            try:
                parent_relative = parent.relative_to(self._root).as_posix()
            except ValueError as exc:
                raise OSError(
                    errno.EPERM,
                    "file mutation mutation guard escaped the workspace",
                ) from exc
            if parent_relative in before and not before[parent_relative].get("exists"):
                expected_after[parent_relative] = {
                    "exists": True,
                    "entry_type": "directory",
                }
            parent = parent.parent
        expected_after[relative] = {
            "exists": True,
            "entry_type": "file",
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        self._persist(expected_after)

    def before_delete(self, path: Path) -> None:
        """校验并记录补丁删除目标的预期缺失状态。

        参数:
            path: 补丁要删除的 workspace 文件，必须已包含在预备快照中。

        返回:
            无。

        异常:
            OSError: 路径越出工作区，或目标状态在预备快照后发生变化。
            ValueError: 目标路径不在预备快照中。
            RuntimeError: 目标的当前状态与快照状态不再匹配。

        副作用:
            读取工作区元数据，并在快照载荷中记录目标的预期缺失状态。
        """
        relative = self._relative(path)
        before = self._payload["paths_before"]
        if relative not in before:
            raise ValueError("delete target is absent from the prepared ChangeSet snapshot")
        self._persist({relative: {"exists": False}})

    def trash_destination(self, relative: str) -> Path | None:
        """返回删除目标在 trash 暂存区内的落盘路径（仅当本操作启用 trash 时）。

        参数:
            relative: 已捕获的工作区相对路径键。

        返回:
            trash 目标绝对路径；未启用 trash 或目标不在预备快照中时为 ``None``。

        异常:
            无。

        副作用:
            无（不创建目录；落盘由调用方在移动时完成）。
        """
        if self._trash_root is None or relative not in self._payload["paths_before"]:
            return None
        return self._trash_root / relative

    def after_parent_directories_created(self, path: Path) -> None:
        """在写入文件前记录新建父目录的身份标识。

        参数:
            path: 处理器创建了父目录的目标文件路径。

        返回:
            无。

        异常:
            OSError: 路径越出工作区。
            ValueError: 路径不在预备快照中。
            RuntimeError: 预备快照无法完成状态转换。

        副作用:
            读取目录身份标识，并写入预备快照载荷。
        """
        target = self._relative(path)
        directory_ids = self._payload.setdefault("directory_ids", {})
        for directory in self._payload.get("implicit_directory_paths", []):
            if not is_relative_ancestor(directory, target):
                continue
            identity = directory_identity(self._root / Path(directory))
            if identity is not None:
                directory_ids[directory] = identity

    def before_move(self, source: Path, destination: Path) -> None:
        """移动文件前校验并记录源路径缺失状态和目标路径状态。

        参数:
            source: 此快照中记录的源路径。
            destination: 此快照中记录的目标路径。

        返回:
            无。

        异常:
            OSError: 任一路径越出工作区。
            ValueError: 任一路径不在预备快照中。
            RuntimeError: 路径的当前状态与预期状态不再匹配。

        副作用:
            读取工作区元数据，并在快照载荷中记录移动后的预期状态。
        """
        source_relative = self._relative(source)
        destination_relative = self._relative(destination)
        before = self._payload["paths_before"]
        if source_relative not in before or destination_relative not in before:
            raise ValueError("move paths are absent from the prepared ChangeSet snapshot")
        source_state = self._payload.get("paths_after", {}).get(
            source_relative,
            before[source_relative],
        )
        destination_state = {
            key: value for key, value in source_state.items() if key != "restore_ref"
        }
        self._persist(
            {
                source_relative: {"exists": False},
                destination_relative: destination_state,
            }
        )

    def _relative(self, path: Path) -> str:
        """将处理器路径解析为快照中已捕获的工作区相对路径键。"""
        absolute = Path(os.path.abspath(path))
        try:
            relative = absolute.relative_to(self._root).as_posix()
        except ValueError as exc:
            raise OSError(
                errno.EPERM, "file mutation mutation guard escaped the workspace"
            ) from exc
        if relative not in self._payload["paths_before"]:
            raise ValueError("file mutation mutation guard path is absent from its snapshot")
        return relative

    def _persist(self, expected_after: Mapping[str, dict[str, Any]]) -> None:
        """校验当前状态，并更新内存中的预期状态清单。"""
        before = self._payload["paths_before"]
        current_expected = self._payload.setdefault("paths_after", {})
        current = self._capture_states(self._root, list(expected_after))
        for path, state in expected_after.items():
            expected_current = current_expected.get(path, before[path])
            if not state_matches(current[path], expected_current):
                raise RuntimeError("workspace path changed before its snapshoted operation")
            current_expected[path] = state


_active_guard: ContextVar[FileMutationGuard | None] = ContextVar(
    "active_file_mutation_guard",
    default=None,
)


@contextmanager
def mutation_guard_scope(guard: FileMutationGuard) -> Iterator[None]:
    """将文件变更守卫绑定到当前同步处理器执行上下文。

    参数:
        guard: 文件工具钩子使用的预备快照变更守卫。

    返回:
        上下文管理器；退出时恢复先前的文件变更守卫。

    异常:
        处理器抛出的异常会在恢复先前上下文后继续向外传播。

    副作用:
        在 ``with`` 代码块期间设置上下文局部的文件变更守卫，并在退出时重置。
    """

    token: Token[FileMutationGuard | None] = _active_guard.set(guard)
    try:
        yield
    finally:
        _active_guard.reset(token)


def before_file_write(path: Path, content: bytes) -> None:
    """处理器写入文件内容前通知当前生效的文件变更守卫。

    参数:
        path: 目标文件路径。
        content: 处理器即将写入的准确字节内容。

    返回:
        无。

    异常:
        文件变更守卫的校验或持久化错误会传播给处理器。

    副作用:
        若文件变更守卫已启用，则更新预备快照；否则无副作用。
    """

    guard = _active_guard.get()
    if guard is not None:
        guard.before_write(path, content)


def before_file_delete(path: Path) -> None:
    """补丁删除文件前通知当前生效的文件变更守卫。

    参数:
        path: 补丁要删除的文件路径。

    返回:
        无。

    异常:
        文件变更守卫的校验或持久化错误会传播给处理器。

    副作用:
        若文件变更守卫已启用，则更新预备快照；否则无副作用。
    """

    guard = _active_guard.get()
    if guard is not None:
        guard.before_delete(path)


def planned_delete_trash_target(path: Path) -> Path | None:
    """返回当前生效守卫为某删除目标规划的 trash 落盘路径。

    参数:
        path: 处理器即将删除的文件绝对路径。

    返回:
        trash 目标路径；无生效守卫、未启用 trash 或路径不在预备快照中时为 ``None``。

    异常:
        无。

    副作用:
        无（仅读取当前上下文的守卫状态）。
    """

    guard = _active_guard.get()
    if guard is None or guard._trash_root is None:
        return None
    try:
        relative = Path(os.path.abspath(path)).relative_to(guard._root).as_posix()
    except ValueError:
        return None
    return guard.trash_destination(relative)


def after_file_parent_directories_created(path: Path) -> None:
    """隐式父目录创建后通知当前生效的文件变更守卫。

    参数:
        path: 已为其创建父目录的目标文件路径。

    返回:
        无。

    异常:
        文件变更守卫的校验或持久化错误会传播给处理器。

    副作用:
        若文件变更守卫已启用，则更新预备快照；否则无副作用。
    """
    guard = _active_guard.get()
    if guard is not None:
        guard.after_parent_directories_created(path)


def before_file_move(source: Path, destination: Path) -> None:
    """处理器移动文件前通知当前生效的文件变更守卫。

    参数:
        source: 源路径。
        destination: 目标路径。

    返回:
        无。

    异常:
        文件变更守卫的校验或持久化错误会传播给处理器。

    副作用:
        若文件变更守卫已启用，则更新预备快照；否则无副作用。
    """

    guard = _active_guard.get()
    if guard is not None:
        guard.before_move(source, destination)
