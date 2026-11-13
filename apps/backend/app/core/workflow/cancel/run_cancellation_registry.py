"""Session 级进程内协作取消信号；不拥有持久状态、模型会话或工具生命周期。"""

from __future__ import annotations

import threading
from typing import Literal

from app.config.constant import Constant
from app.config.logging.logger import log

CancelReason = Literal["user", "parent"]
"""取消原因：用户停止（打断当前驱动）、上游（父 Agent）停止（另外收回 Session 交出去的工作，见
``SessionActor.cancel``）。落库成不同的结束原因（:func:`cancel_end_reason`），只有用户停止会在下一次驱动
推导出「用户已停止」通知。"""

_END_REASONS: dict[CancelReason, str] = {
    "user": Constant.Run.RUN_END_REASON_USER_CANCELLED,
    "parent": Constant.Run.RUN_END_REASON_PARENT_CANCELLED,
}


def cancel_end_reason(reason: CancelReason) -> str:
    """取消原因落库为 Run 的哪个结束原因；所有 ``RunTransition.CANCEL``（重启收敛除外）都经这里。"""
    return _END_REASONS[reason]


class RunCancellationRegistry:
    """记录当前进程内「某 Session 的当前执行应停止」的信号。

    按 session_id 而非 run_id 记录：同一 Session 内 Run 串行、同时只有一个执行者，停止的语义是
    「停下这个 Session 正在做的事」，命中的永远是当前执行，不受前端 run_id 滞后影响。

    本类只发信号、不做状态迁移：Agent 循环在 MODEL 检查点、工具在执行边界各自读取信号并
    收敛，Run 终态仍由 workflow 写入。信号生命周期由驱动方保证——只在 Session 存在活动执行时
    标记，执行释放时经 :meth:`consume` 原子取走（执行器还据此判断释放瞬间是否有迟到的取消），
    否则残留信号会让同一 Session 的下一次执行一启动就被取消。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # session_id → 取消原因；释放时连同原因一起取走。
        self._reasons: dict[int, CancelReason] = {}

    def request_cancel(self, session_id: int, reason: CancelReason = "user") -> None:
        """记录取消意图与原因；幂等，可从工具线程调用。

        已有信号时只允许 ``user`` 升级为 ``parent``：用户打断途中上游又收回，后台结果登记表要按收回
        拒收这次驱动剩下的登记；反过来不降级。
        """
        with self._lock:
            current = self._reasons.get(session_id)
            if current == reason or current == "parent":
                return
            self._reasons[session_id] = reason
        log.info(
            "session_cancellation_requested",
            extra={
                "msg": "Session 取消信号已登记",
                "data": {"session_id": session_id, "reason": reason},
            },
        )

    def is_cancelled(self, session_id: int) -> bool:
        """有任一原因的信号：当前执行应停下。"""
        with self._lock:
            return session_id in self._reasons

    def reason(self, session_id: int) -> CancelReason | None:
        """读当前信号的原因、不取走：驱动内的检查点据此落定结束原因，信号仍留给释放边界取走。"""
        with self._lock:
            return self._reasons.get(session_id)

    def consume(self, session_id: int) -> CancelReason | None:
        """取走信号并返回取走前的原因（没有信号为 ``None``）；只由执行释放边界调用。"""
        with self._lock:
            return self._reasons.pop(session_id, None)


cancellation_registry = RunCancellationRegistry()
