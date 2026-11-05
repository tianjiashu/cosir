"""耗时工作的结论交付与撤销：终端线程作业和子 Agent 等待共用。

作业由执行单元持有（终端会话、子 Task 的 actor 等），只承诺两件事：结论到达时回调恰好一次，以及
可以被撤销。结果登记与回投归 ``BackgroundRegistry``，前后台选择与观察组装归各工具；
终端独有的前台等待和转后台由 ``ExecuteTerminalTool`` 编排。

两种包装方式共用一个类，差别只在「取消时结论由谁产生」（见 :class:`BackgroundJob`）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from app.config.logging.logger import log


@dataclass(frozen=True)
class BackgroundConclusion:
    """作业的结论：给模型的 ``result`` 与可选的卡片终态 ``display_data``。

    两者分开是因为受众不同：``result`` 以 ``background_result`` 回投给模型（会过输出预算），
    ``display_data`` 只在结论并入时写回后台占位卡片（UI 展示契约，不进模型、不截断）。结论只走一条
    路径（作业 → 路由 → 登记表 → 消息），所以卡片数据随结论一起走，不另开通道。没有卡片终态的作业
    （子 Agent：卡片跟随子 Task）留 ``None``，占位沿用原展示数据。
    """

    result: dict[str, object]
    display_data: dict[str, object] | None = None
    cancel_hint: str | None = None


JobCallback = Callable[[BackgroundConclusion], None]


class _OnceCallbacks:
    """结论只交付一次；先到的结论缓存，之后注册的回调立即拿到它。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._result: BackgroundConclusion | None = None
        self._callbacks: list[JobCallback] = []
        self._done = False

    def on_done(self, callback: JobCallback) -> None:
        with self._lock:
            if not self._done:
                self._callbacks.append(callback)
                return
            result = cast(BackgroundConclusion, self._result)
        callback(result)

    def finish(self, result: BackgroundConclusion) -> bool:
        """交付结论；已交付过返回 False（迟到的第二个结论丢弃）。"""
        with self._lock:
            if self._done:
                return False
            self._done, self._result = True, result
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback(result)
        return True


class BackgroundJob:
    """一份进行中的耗时工作：结论恰好交付一次，可撤销。

    两种包装方式的唯一差别是取消时结论由谁产生：

    - :meth:`start_in_thread`：阻塞函数放在自己的线程里跑，取消只唤醒它，结论由它返回（可以带上部分
      输出，如被停止的命令）。框架不代结：它先交付会让前台等待方拿到结论，而执行结果还没写回。
    - 直接构造：结论由执行单元在合适时机 :meth:`finish`；取消时框架立即以 ``cancelled_result``
      收尾——等这个事实的等待方会随撤销消失，没有人会再交付，所以 ``cancelled_result`` 必填。
    """

    def __init__(
        self,
        *,
        on_cancel: Callable[[], None],
        cancelled_result: BackgroundConclusion | None = None,
    ) -> None:
        """``on_cancel`` 在撤销标记置位后、锁外调用，每次 ``cancel`` 都调，负责停命令或取消等待。
        ``cancelled_result`` 为 ``None`` 表示取消后等执行单元自己 :meth:`finish`；
        给了就以它立刻收尾，保证等待方总能拿到结论。
        """
        self._callbacks = _OnceCallbacks()
        self._cancelled = threading.Event()
        self._on_cancel = on_cancel
        self._cancelled_result = cancelled_result

    @classmethod
    def start_in_thread(
        cls,
        work: Callable[[Callable[[], bool]], BackgroundConclusion],
        *,
        wake: Callable[[], None],
        name: str,
    ) -> BackgroundJob:
        """把阻塞函数 ``work(is_cancelled)`` 放到自己的线程里执行（``execute_terminal`` 的命令）。

        撤销置位标记并调用 ``wake`` 唤醒工作，工作须尽快以取消结论返回；
        可轮询 ``is_cancelled`` 或由唤醒回调直接停止执行后端。线程名用于排查。
        """
        job = cls(on_cancel=wake)
        threading.Thread(target=job._run, args=(work,), name=name, daemon=True).start()
        return job

    def finish(self, result: BackgroundConclusion) -> bool:
        """交付结论；已交付过返回 False（迟到的第二个结论丢弃）。调用方负责线程：回调可能写 SQLite，
        不得在 PTY 读线程里调用。"""
        return self._callbacks.finish(result)

    def on_done(self, callback: JobCallback) -> None:
        """注册结论回调：结论（含被撤销后的取消结论）到达时调用恰好一次。

        已经有结论时立即在调用方线程回调。回调可能写 SQLite，作业不得在 PTY 读线程等热路径上
        触发它。
        """
        self._callbacks.on_done(callback)

    def is_cancelled(self) -> bool:
        """是否已被撤销；只有 :meth:`start_in_thread` 的阻塞工作需要轮询它。"""
        return self._cancelled.is_set()

    def cancel(self) -> None:
        """撤销：置位标记、通知执行单元，给了 ``cancelled_result`` 就立刻以它收尾。幂等。"""
        self._cancelled.set()
        self._on_cancel()
        if self._cancelled_result is not None:
            self.finish(self._cancelled_result)

    def _run(self, work: Callable[[Callable[[], bool]], BackgroundConclusion]) -> None:
        try:
            conclusion = work(self.is_cancelled)
        except Exception as exc:
            log.exception("background_job_failed", extra={
                "msg": "后台工具执行异常", "data": {"thread": threading.current_thread().name},
            })
            conclusion = BackgroundConclusion({"end_reason": "failed", "error": str(exc)})
        self.finish(conclusion)
