"""测试用的命令作业替身：按 ``CommandJob`` 的鸭子类型登记进 ``command_results``，不起线程、不跑命令。"""

from __future__ import annotations

from collections.abc import Callable

from app.core.tool.placeholder_settlement import USER_CANCELLED_HINT
from app.tools.tool_handler.terminal.command_job import CommandConclusion, JobCallback

CANCELLED = CommandConclusion({"end_reason": "cancelled"}, cancel_hint=USER_CANCELLED_HINT)


class FakeCommandJob:
    """``cancel`` 默认像真实命令一样以取消结论收尾（同步交付，便于断言）；``finish`` 交付任意结论。

    ``on_cancel`` 用来观察撤销；``finish_on_cancel=False`` 模拟停不下来、迟迟不交付的作业。
    """

    def __init__(
        self,
        on_cancel: Callable[[], None] | None = None,
        *,
        finish_on_cancel: bool = True,
        cancelled: CommandConclusion = CANCELLED,
    ) -> None:
        self.cancels = 0
        self._on_cancel = on_cancel
        self._finish_on_cancel = finish_on_cancel
        self._cancelled = cancelled
        self._conclusion: CommandConclusion | None = None
        self._callbacks: list[JobCallback] = []

    def cancel(self) -> None:
        self.cancels += 1
        try:
            if self._on_cancel is not None:
                self._on_cancel()
        finally:
            # 观察钩子抛错也照样收尾：真实作业的结论由命令线程交付，不受停止回调影响。
            if self._finish_on_cancel:
                self.finish(self._cancelled)

    def finish(self, conclusion: CommandConclusion) -> None:
        if self._conclusion is not None:
            return
        self._conclusion = conclusion
        callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback(conclusion)

    def on_done(self, callback: JobCallback) -> None:
        if self._conclusion is None:
            self._callbacks.append(callback)
            return
        callback(self._conclusion)

    def wait(self, timeout: float | None) -> CommandConclusion | None:
        return self._conclusion
