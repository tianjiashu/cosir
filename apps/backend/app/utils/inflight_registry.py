"""按 key 去重的「进行中调用」注册表（通用 singleflight 并发原语）。

单一职责：保证**同一 key 的并发调用只真正执行一次**，其余并发调用等待并共享
首次执行的同一结果（成功或异常）。这是经典的 singleflight（单飞）模式，与具体
业务领域无关，是通用的并发原语。

适用场景：多个并发请求可能同时触发同一份「重活」（如建立 CodeGraph 索引、
初始化共享资源），需要合并为一次真实执行，避免重复计算 / 互相干扰 / 竞态。

职责边界：
- 负责：按 key 去重「进行中」的调用，leader/follower 的判定与结果/异常共享。
- 不负责：
  - 结果缓存（调用结束即移除 key，下次同 key 重新执行）；
  - key 的语义规范化（绝对路径、大小写归一等由调用方负责，本类按原样比对）；
  - 并发执行策略（本类是同步原语，``fn`` 在 leader 的调用线程内执行；
    「放到线程/进程池」由调用方决定）。

线程安全：内部用 ``threading.Lock`` 保护 ``key -> Future`` 映射，所有注册表操作
（查询/占位/移除）都在锁内完成，``fn`` 的执行在锁外，不持锁阻塞其它线程。
可在多线程下安全使用。

标准库说明：Python 标准库**没有**开箱即用的 singleflight 原语（Go 的
``x/sync/singleflight`` 是成熟参考）。``functools.lru_cache`` 语义不符（它会
缓存结果，而本类是「进行中」去重 + 结果不缓存）；``asyncio`` 的 Future/Lock
仅适用于异步场景。因此用标准库组件（``threading.Lock`` +
``concurrent.futures.Future``）组合实现本原语，属于合理的手写（未重复造底层）。
"""

import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, TypeVar

T = TypeVar("T")


class InflightRegistry:
    """按 key 去重「进行中」调用的注册表（同步 singleflight）。

    同 key 并发调用只执行一次（leader）；其余调用（follower）拿到首次执行的同一
    结果。调用结束（成功或异常）后移除 key，**不做结果缓存**——下次同 key 调用
    会重新执行。

    典型用法（见 ``CodeGraphLifecycleService.ensure_ready``）：对同一 workspace
    并发多次 ensure_ready，只真正执行一次索引就绪编排，其余等待共享结果。
    """

    def __init__(self) -> None:
        """构造空注册表。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """
        self._inflight: dict[str, Future[Any]] = {}
        self._lock = threading.Lock()

    def run(self, key: str, fn: Callable[[], T]) -> T:
        """以 singleflight 方式执行 ``fn``：同 key 并发只执行一次，共享结果。

        在同一 key 的并发调用中，第一个到达的线程成为 **leader**，真正执行 ``fn``；
        其余线程成为 **follower**，不执行 ``fn``，而是阻塞等待 leader 完成并拿到
        同一结果（``Future.result()``）。leader 执行完成后移除 key，因此**结果不
        缓存**：下次同 key 调用会重新执行。

        参数:
            key: 去重键。调用方负责规范化（如 Windows 绝对路径经
                ``os.path.normcase`` 归一），本类按原样（dict 键）比对。
            fn: 待执行的零参可调用对象，返回 ``T``。在 leader 的调用线程内同步执行，
                不新起线程/进程。

        返回:
            ``fn`` 的执行结果；follower 返回与 leader 相同的对象。

        异常:
            透传 ``fn`` 抛出的异常：leader 在 ``fn`` 抛异常时移除 key 并 re-raise；
                follower 经 ``future.result()`` 抛出**与 leader 相同的异常**（所有
                并发调用方一致感知失败）。

        副作用:
            - 并发同 key 调用共享首次执行结果（success 或 exception）；
            - 调用结束（leader 完成）后移除 key，不保留缓存。
        """
        leader: bool
        with self._lock:
            existing = self._inflight.get(key)
            if existing is not None and not existing.done():
                # 已有进行中的同 key 调用：出锁后等待同一结果。
                future = existing
                leader = False
            else:
                future = Future()
                self._inflight[key] = future
                leader = True
        if leader:
            try:
                result = fn()
            except BaseException as exc:
                with self._lock:
                    self._inflight.pop(key, None)
                if not future.done():
                    future.set_exception(exc)
                raise
            with self._lock:
                self._inflight.pop(key, None)
            future.set_result(result)
            return result
        return future.result()
