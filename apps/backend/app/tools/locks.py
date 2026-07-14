"""并发工具调用的资源锁。"""

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Iterable, Iterator


class ToolResourceLockManager:
    """在单个后端进程中串行化冲突工具资源。"""

    def __init__(self) -> None:
        """初始化空的资源锁集合。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            创建进程内锁注册表。
        """

        self._locks: dict[str, RLock] = {}
        self._guard = RLock()

    def normalize_keys(self, resource_keys: Iterable[str]) -> tuple[str, ...]:
        """规范化并去重资源锁键。

        参数:
            resource_keys: 工具声明的资源键。

        返回:
            排序后的规范化资源键元组。

        异常:
            无。

        副作用:
            无。
        """

        normalized = set()
        for key in resource_keys:
            if key.startswith("file:"):
                normalized.add(f"file:{Path(key[5:]).resolve()}")
            else:
                normalized.add(key.strip())
        return tuple(sorted(key for key in normalized if key))

    @contextmanager
    def acquire(self, resource_keys: Iterable[str]) -> Iterator[None]:
        """按稳定顺序获取一组资源锁。

        参数:
            resource_keys: 需要独占的资源键。

        返回:
            可在 with 语句中运行的无值上下文。

        异常:
            无。

        副作用:
            获取并最终释放进程内资源锁。
        """

        keys = self.normalize_keys(resource_keys)
        locks = [self._get_lock(key) for key in keys]
        for lock in locks:
            lock.acquire()
        try:
            yield
        finally:
            for lock in reversed(locks):
                lock.release()

    def _get_lock(self, key: str) -> RLock:
        """返回指定规范化键的进程内可重入锁。

        参数:
            key: 已规范化资源键。

        返回:
            稳定的可重入锁对象。

        异常:
            无。

        副作用:
            第一次访问时创建并缓存锁。
        """

        with self._guard:
            return self._locks.setdefault(key, RLock())
