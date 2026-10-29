"""延迟结果的作业抽象：工具把耗时工作包装成作业，编排交给 ``run_deferred``。

设计见 ``docs/plan/deferred-tool-result-design.md``「作业与统一执行入口」。作业由执行单元持有（终端
会话、子 Task 的 actor 等），只承诺两件事：结论到达时回调恰好一次，以及可以被撤销。登记、前台 /
后台、转到后台、回投与占位都不归作业管——那是 ``run_deferred`` 的职责。

为什么是抽象基类：使用者有终端条件等待、终端交接、子 Run（之后还有 Team），只有两种形状，所以
本模块给出两个通用实现——``ThreadJob``（阻塞函数在自己的线程里跑，终端条件等待）与 ``ManualJob``
（执行单元显式交付，终端交接与子 Run），``run_deferred`` 只认抽象基类。工具侧只构造它们并接上自己的
执行单元，不再各写一套作业类。
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

JobCallback = Callable[[dict[str, object]], None]

# 占位原因：``background`` 一开始就在后台执行；``detached`` 用户把前台等待转走；``limit`` 前台等待
# 到达工具声明的上限、自动转后台。是否挂起 Agent 由工具按原因决定（见各工具的占位渲染）。
PlaceholderReason = Literal["background", "detached", "limit"]


class DeferredJob(ABC):
    """一份进行中的耗时工作；实现显式继承本类（``ThreadJob`` / ``ManualJob``）。"""

    @abstractmethod
    def on_done(self, callback: JobCallback) -> None:
        """注册结论回调：结论（含被撤销后的取消结论）到达时调用恰好一次。

        已经有结论时立即在调用方线程回调。回调可能写 SQLite，作业不得在 PTY 读线程等热路径上
        触发它。
        """

    @abstractmethod
    def cancel(self) -> None:
        """撤销：停止工作或放弃等待；作业随后以取消结论回调（撤销后的结论由登记表丢弃）。"""


@dataclass(frozen=True)
class DeferredOutcome:
    """``run_deferred`` 的结果：要么前台拿到的结论，要么一个后台占位的原因。

    ``run_deferred`` 不渲染工具观察：结论与占位的渲染（文本、展示数据、是否挂起）属于工具。工具渲染
    占位时必须标记 ``deferred``，卡片状态以占位行为准。
    """

    result: dict[str, object] | None = None
    placeholder: PlaceholderReason | None = None


class _OnceCallbacks:
    """结论只交付一次；先到的结论缓存，之后注册的回调立即拿到它。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._result: dict[str, object] | None = None
        self._callbacks: list[JobCallback] = []
        self._done = False

    def on_done(self, callback: JobCallback) -> None:
        with self._lock:
            if not self._done:
                self._callbacks.append(callback)
                return
            result = cast(dict[str, object], self._result)
        callback(result)

    def finish(self, result: dict[str, object]) -> bool:
        """交付结论；已交付过返回 False（迟到的第二个结论丢弃）。"""
        with self._lock:
            if self._done:
                return False
            self._done, self._result = True, result
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback(result)
        return True


class ThreadJob(DeferredJob):
    """把一个阻塞函数放到自己的线程里执行的作业（如终端会话上的条件等待）。

    ``work(is_cancelled)`` 返回结论；撤销只置位标记并调用 ``wake`` 唤醒它（例如通知会话条件变量），
    ``work`` 须据 ``is_cancelled`` 尽快以取消结论返回。线程名用于排查。
    """

    def __init__(
        self,
        work: Callable[[Callable[[], bool]], dict[str, object]],
        *,
        wake: Callable[[], None],
        name: str,
    ) -> None:
        self._callbacks = _OnceCallbacks()
        self._cancelled = threading.Event()
        self._wake = wake
        threading.Thread(target=self._run, args=(work,), name=name, daemon=True).start()

    def _run(self, work: Callable[[Callable[[], bool]], dict[str, object]]) -> None:
        self._callbacks.finish(work(self._cancelled.is_set))

    def on_done(self, callback: JobCallback) -> None:
        self._callbacks.on_done(callback)

    def cancel(self) -> None:
        self._cancelled.set()
        self._wake()


class ManualJob(DeferredJob):
    """由执行单元在合适时机显式 ``finish`` 的作业（如终端交接、子 Agent 的一次 Run）。

    撤销调用 ``on_cancel``（如清掉交接提示、停止子 Task），再以 ``cancelled_result`` 收尾，保证前台
    等待方总能拿到结论。``finish`` 的调用方负责线程：回调可能写 SQLite，不得在 PTY 读线程里调用。
    """

    def __init__(
        self, *, on_cancel: Callable[[], None], cancelled_result: dict[str, object]
    ) -> None:
        self._callbacks = _OnceCallbacks()
        self._on_cancel = on_cancel
        self._cancelled_result = cancelled_result

    def finish(self, result: dict[str, object]) -> bool:
        return self._callbacks.finish(result)

    def on_done(self, callback: JobCallback) -> None:
        self._callbacks.on_done(callback)

    def cancel(self) -> None:
        self._on_cancel()
        self._callbacks.finish(self._cancelled_result)
