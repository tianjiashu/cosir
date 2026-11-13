"""Backend 进程内 workspace 级操作闸门。"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class WorkspaceOperationRegistry:
    """为同一 workspace 的结构性写操作提供稳定的进程内互斥锁。"""

    def __init__(self) -> None:
        """初始化空的 workspace 锁注册表。"""

        self._locks: dict[int, threading.Lock] = {}
        self._guard = threading.Lock()

    def _get_lock(self, workspace_id: int) -> threading.Lock:
        """返回指定 workspace 的稳定锁实例。"""

        with self._guard:
            lock = self._locks.get(workspace_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[workspace_id] = lock
            return lock

    @contextmanager
    def operation(self, workspace_id: int, timeout: float | None = None) -> Iterator[None]:
        """同步取得 workspace 操作闸门。

        参数:
            workspace_id: workspace 标识。
            timeout: 最长等待秒数；为 ``None`` 时持续等待。

        返回:
            一个持有 workspace 锁的上下文管理器。

        异常:
            TimeoutError: 在指定时间内未能取得 workspace 闸门。

        副作用:
            在上下文期间阻止同一 workspace 的结构性写操作。
        """

        lock = self._get_lock(workspace_id)
        acquired = lock.acquire() if timeout is None else lock.acquire(timeout=timeout)
        if not acquired:
            raise TimeoutError(f"workspace {workspace_id} operation lock is busy")
        try:
            yield
        finally:
            lock.release()

    def close(self) -> None:
        """清空已登记的 workspace 锁。

        调用方必须保证 backend 中没有仍持有这些锁的操作。
        """

        with self._guard:
            self._locks.clear()


workspace_operations = WorkspaceOperationRegistry()
