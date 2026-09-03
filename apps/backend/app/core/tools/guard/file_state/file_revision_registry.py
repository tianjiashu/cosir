"""按 task 维护有界文件 revision 快照。"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileFingerprint:
    """文件存在性、修改时间与大小组成的轻量 revision。"""

    exists: bool
    mtime_ns: int
    size: int


class FileRevisionRegistry:
    """维护 task 隔离、LRU 有界的文件 revision。"""

    def __init__(
        self,
        *,
        max_tasks: int = 128,
        max_paths_per_task: int = 2_048,
    ) -> None:
        """初始化 revision registry。

        参数:
            max_tasks: 最多保留的 task 数。
            max_paths_per_task: 每个 task 最多保留的路径数。

        返回:
            无。

        异常:
            ValueError: 任一容量小于 1 时抛出。

        副作用:
            创建进程内锁与空的 LRU 状态。
        """

        if max_tasks < 1 or max_paths_per_task < 1:
            raise ValueError("revision registry capacities must be greater than zero")
        self._max_tasks = max_tasks
        self._max_paths_per_task = max_paths_per_task
        self._tasks: OrderedDict[str, OrderedDict[str, FileFingerprint]] = OrderedDict()
        self._lock = threading.RLock()

    def record(self, task_id: str, paths: Iterable[Path]) -> None:
        """记录一组路径的当前 fingerprint。

        参数:
            task_id: 状态所属任务。
            paths: 待记录路径集合。

        返回:
            无。

        异常:
            无。

        副作用:
            更新 task 的路径 LRU，并在超限时淘汰最旧状态。
        """

        snapshots = [(self.canonical_path(path), self.fingerprint(path)) for path in paths]
        self.record_snapshots(task_id, snapshots)

    def record_snapshots(
        self,
        task_id: str,
        snapshots: Iterable[tuple[str, FileFingerprint]],
    ) -> None:
        """记录调用方在确定时刻取得的 fingerprint 快照。

        参数:
            task_id: 状态所属任务。
            snapshots: canonical path 与当时 fingerprint 的二元组。

        返回:
            无。

        异常:
            无。

        副作用:
            更新 task 的路径 LRU，并在超限时淘汰最旧状态。
        """

        with self._lock:
            task_state = self._task_state(task_id)
            for canonical, fingerprint in snapshots:
                task_state[canonical] = fingerprint
                task_state.move_to_end(canonical)
                while len(task_state) > self._max_paths_per_task:
                    task_state.popitem(last=False)

    def stale_paths(self, task_id: str, paths: Iterable[Path]) -> tuple[Path, ...]:
        """返回已记录且当前 fingerprint 已变化的路径。

        参数:
            task_id: 状态所属任务。
            paths: 写、改、删前待检查的路径。

        返回:
            发生变化的路径元组；从未记录的路径不视为 stale。

        异常:
            无。

        副作用:
            读取并刷新命中 task/path 的 LRU 顺序。
        """

        candidates = [(path, self.canonical_path(path)) for path in paths]
        stale: list[Path] = []
        with self._lock:
            task_state = self._tasks.get(task_id)
            if task_state is None:
                return ()
            self._tasks.move_to_end(task_id)
            for path, canonical in candidates:
                previous = task_state.get(canonical)
                if previous is None:
                    continue
                task_state.move_to_end(canonical)
                if previous != self.fingerprint(path):
                    stale.append(path)
        return tuple(stale)

    def snapshot_token(self, paths: Iterable[Path]) -> tuple[tuple[str, FileFingerprint], ...]:
        """构造可比较的路径集合 fingerprint token。

        参数:
            paths: 待快照的路径。

        返回:
            按 canonical path 排序的不可变 ``(path, fingerprint)`` 元组。

        异常:
            无。

        副作用:
            仅读取路径元数据。
        """

        snapshots = {self.canonical_path(path): self.fingerprint(path) for path in paths}
        return tuple(sorted(snapshots.items(), key=lambda item: item[0]))

    def clear_task(self, task_id: str) -> None:
        """清除一个 task 的全部 revision。

        参数:
            task_id: 待清除任务。

        返回:
            无。

        异常:
            无。

        副作用:
            删除进程内 task 状态。
        """

        with self._lock:
            self._tasks.pop(task_id, None)

    @staticmethod
    def canonical_path(path: Path) -> str:
        """返回稳定 canonical path 字符串。

        参数:
            path: 待规范化路径。

        返回:
            不要求目标存在的绝对规范路径字符串。

        异常:
            无。

        副作用:
            无。
        """

        return os.path.normcase(os.path.abspath(path))

    @staticmethod
    def fingerprint(path: Path) -> FileFingerprint:
        """读取单个路径的轻量 fingerprint。

        参数:
            path: 待读取路径。

        返回:
            存在时包含 ``mtime_ns`` 和 ``size``；不存在或 stat 失败时返回不存在快照。

        异常:
            无。

        副作用:
            仅读取文件系统元数据。
        """

        try:
            stat = Path(path).stat()
        except (OSError, ValueError):
            return FileFingerprint(exists=False, mtime_ns=0, size=0)
        return FileFingerprint(exists=True, mtime_ns=stat.st_mtime_ns, size=stat.st_size)

    def _task_state(self, task_id: str) -> OrderedDict[str, FileFingerprint]:
        """取得或创建 task 状态并执行 task 级 LRU 淘汰。

        参数:
            task_id: 状态所属任务。

        返回:
            task 对应的有序路径字典。

        异常:
            无。

        副作用:
            可能创建 task 状态并淘汰最旧 task。
        """

        task_state = self._tasks.get(task_id)
        if task_state is None:
            task_state = OrderedDict()
            self._tasks[task_id] = task_state
        self._tasks.move_to_end(task_id)
        while len(self._tasks) > self._max_tasks:
            self._tasks.popitem(last=False)
        return task_state
