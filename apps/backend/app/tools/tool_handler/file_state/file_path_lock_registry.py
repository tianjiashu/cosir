"""按 task/path 提供有界、确定顺序的进程内路径锁。"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from app.tools.tool_handler.file_state.file_revision_registry import (
    FileRevisionRegistry,
)


@dataclass
class _PathLockEntry:
    """单条路径锁及其活跃调用引用数。"""

    lock: threading.RLock = field(default_factory=threading.RLock)
    users: int = 0


@dataclass
class _TaskLockState:
    """单个 task 的路径锁状态。"""

    paths: OrderedDict[str, _PathLockEntry] = field(default_factory=OrderedDict)
    users: int = 0


class FilePathLockRegistry:
    """维护 task 隔离的路径锁并按 canonical path 排序获取多锁。"""

    def __init__(self, *, max_tasks: int = 128, max_paths_per_task: int = 2_048) -> None:
        """初始化路径锁 registry。

        参数:
            max_tasks: 最多保留的 task 数。
            max_paths_per_task: 每个 task 最多创建的路径锁数。

        返回:
            无。

        异常:
            ValueError: 任一容量小于 1 时抛出。

        副作用:
            创建进程内 registry 锁。
        """

        if max_tasks < 1 or max_paths_per_task < 1:
            raise ValueError("path lock registry capacities must be greater than zero")
        self._max_tasks = max_tasks
        self._max_paths_per_task = max_paths_per_task
        self._tasks: OrderedDict[str, _TaskLockState] = OrderedDict()
        self._registry_lock = threading.RLock()

    @contextmanager
    def acquire(self, task_id: str, paths: Sequence[Path]) -> Iterator[None]:
        """按稳定顺序获取一组路径锁，并在退出时逆序释放。

        参数:
            task_id: 锁所属任务。
            paths: 待串行化的写、改、删路径。

        返回:
            上下文管理器；进入后调用方独占这些 task/path 锁。

        异常:
            RuntimeError: 单 task 路径锁容量耗尽时抛出。

        副作用:
            创建、获取并释放进程内 ``RLock``。
        """

        canonical_paths = sorted({FileRevisionRegistry.canonical_path(path) for path in paths})
        entries = self._entries_for(task_id, canonical_paths)
        acquired: list[threading.RLock] = []
        try:
            for entry in entries:
                entry.lock.acquire()
                acquired.append(entry.lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()
            self._release_entries(task_id, entries)

    def clear_task(self, task_id: str) -> None:
        """清除没有活跃持有者时的 task 路径锁引用。

        参数:
            task_id: 待清除任务。

        返回:
            无。

        异常:
            无。

        副作用:
            删除 registry 对 task 锁字典的引用；已取得的锁对象仍由调用栈持有。
        """

        with self._registry_lock:
            state = self._tasks.get(task_id)
            if state is not None and state.users == 0:
                self._tasks.pop(task_id, None)

    def _entries_for(
        self,
        task_id: str,
        canonical_paths: Sequence[str],
    ) -> list[_PathLockEntry]:
        """取得并引用一组 canonical path 对应的锁条目。

        参数:
            task_id: 锁所属任务。
            canonical_paths: 已排序去重的 canonical path。

        返回:
            与输入顺序一致的锁列表。

        异常:
            RuntimeError: 没有可安全淘汰的空闲 task/path 时抛出。

        副作用:
            创建或复用 task/path 锁，增加活跃引用并执行空闲项 LRU 淘汰。
        """

        with self._registry_lock:
            state = self._tasks.get(task_id)
            if state is None:
                if len(self._tasks) >= self._max_tasks:
                    idle_task = next(
                        (
                            existing_task_id
                            for existing_task_id, existing_state in self._tasks.items()
                            if existing_state.users == 0
                        ),
                        None,
                    )
                    if idle_task is None:
                        raise RuntimeError("path lock task capacity exceeded")
                    self._tasks.pop(idle_task)
                state = _TaskLockState()
                self._tasks[task_id] = state

            missing = [path for path in canonical_paths if path not in state.paths]
            required_evictions = max(
                0,
                len(state.paths) + len(missing) - self._max_paths_per_task,
            )
            idle_paths = [
                canonical
                for canonical, entry in state.paths.items()
                if entry.users == 0 and canonical not in canonical_paths
            ]
            if len(idle_paths) < required_evictions:
                raise RuntimeError("path lock capacity exceeded for task")
            for canonical in idle_paths[:required_evictions]:
                state.paths.pop(canonical)
            for canonical in missing:
                state.paths[canonical] = _PathLockEntry()

            entries = [state.paths[canonical] for canonical in canonical_paths]
            for canonical in canonical_paths:
                state.paths[canonical].users += 1
                state.paths.move_to_end(canonical)
            state.users += 1
            self._tasks.move_to_end(task_id)
            return entries

    def _release_entries(self, task_id: str, entries: Sequence[_PathLockEntry]) -> None:
        """释放一次 acquire 对锁条目的活跃引用。

        参数:
            task_id: 锁所属任务。
            entries: :meth:`_entries_for` 返回的锁条目。

        返回:
            无。

        异常:
            无。

        副作用:
            递减 path/task 活跃引用，使其可被后续 LRU 安全淘汰。
        """

        with self._registry_lock:
            state = self._tasks.get(task_id)
            if state is None:
                return
            for entry in entries:
                entry.users = max(0, entry.users - 1)
            state.users = max(0, state.users - 1)
