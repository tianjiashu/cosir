"""Session 的执行登记：至多一个后台执行，与 Session 取消信号的打出、取走构成闭环。

由该 Session 的 ``SessionActor`` 持有。actor 决定「什么时候开、开完怎么接续」，本类只负责「这一次执行」：
登记、驱动、释放，以及取消信号在执行边界上的生命周期。

信号的不变量只在这里成立：**有执行者才打信号，注销执行时一并取走信号**，两步各自之间没有 await。
:meth:`cancel` 判定 ``driving`` 与打信号是同步的，:meth:`_drive` 注销执行与 ``consume`` 也是同步的，
二者在事件循环上互斥——信号要么被这次执行的收尾取走，要么根本不打，不会残留到下一个执行（残留
信号会让下一个 Run 一启动就被取消）。没有执行者时取消该做什么（挂起 Run 的迁移）不归这里，由 actor
在 Session 闸门内处理。

本类不碰后台工作：用户停止只打断这次驱动，上游停止的收回由 actor 在打信号之后另做
（``SessionRecall``）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable

from app.config.logging.logger import log
from app.core.workflow.cancel.run_cancellation_registry import (
    CancelReason,
    cancellation_registry,
)
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.core.workflow.message_queue.session_message import SessionMessage
from app.core.workflow.run_driver import RunDriver
from app.models import RunRecord

OnReleased = Callable[[RunRecord, CancelReason | None], Awaitable[None]]
"""执行释放后的回调：收到这次 Run 与收尾时取走的取消原因（没收到取消为 ``None``）。"""


class SessionExecution:
    """一个 Session 的执行登记；方法只能在事件循环线程调用。"""

    def __init__(
            self,
            session_id: int,
            mailbox: RunMailbox,
            on_released: OnReleased,
    ) -> None:
        self.session_id = session_id
        self._mailbox = mailbox
        # 释放后的处理（传播、接续）是调度判定，归 actor；这里只在释放后交出去。
        self._on_released = on_released
        # 当前后台执行（驱动一个 Run 的子 session）及其 Run；没有执行时为 ``None``，注销时一起清空。
        self._task: asyncio.Task[None] | None = None
        self._run: RunRecord | None = None

    @property
    def driving(self) -> bool:
        """是否有执行者；同步判定，不读库——打取消信号必须与执行收尾之间没有 await。"""
        return self._task is not None and not self._task.done()

    @property
    def stopping(self) -> bool:
        """有执行者且已收到取消信号：这次执行即将作废，不应再往 mailbox 投递。"""
        return self.driving and cancellation_registry.is_cancelled(self.session_id)

    def start(
        self,
        run: RunRecord,
        initial: Iterable[SessionMessage],
        preceding_run: RunRecord | None,
    ) -> None:
        """清空 mailbox、投递初始消息，再把 Run 交给后台执行；调用方持 Session 闸门。

        ``preceding_run`` 原样交给驱动（见 ``RuntimeContext.preceding_run``）。

        执行作为独立的子 session 运行，调用方不等它：调度 loop 要继续监听（运行中到达的消息才能及时
        投进 mailbox），闸门也不能被整个 Run 占住。

        异常:
            ValueError: 已有执行（调用方违约）。
        """
        if self.driving:
            raise ValueError(f"session {self.session_id} 已有执行")
        self._mailbox.reset()
        for message in initial:
            self._mailbox.post(message)
        self._run = run
        self._task = asyncio.get_running_loop().create_task(self._drive(run, preceding_run))
        # 兜底：释放边界之外的异常在这里留下日志，不留无人取回失败的 session。
        self._task.add_done_callback(self._on_drive_done)

    def cancel(self, reason: CancelReason) -> bool:
        """有执行者时打 Session 取消信号，返回是否打了；没有执行者时什么也不登记（挂起的 Run 归 actor）。"""
        if not self.driving:
            return False
        cancellation_registry.request_cancel(self.session_id, reason)
        return True

    async def wait_released(self) -> None:
        """等当前执行（含释放后的回调）结束；没有执行时立即返回。

        用 ``asyncio.wait`` 而不是直接 await：等待方被取消时不连带取消执行。
        """
        if self._task is not None and not self._task.done():
            await asyncio.wait({self._task})

    async def abort(self) -> None:
        """进程关闭：直接取消执行，不等工作流落定；遗留 running Run 由下次启动收敛。

        执行被直接取消，收尾与释放回调都不会运行，所以在这里补取信号。
        """
        if not self.driving:
            return
        session = self._task
        cancellation_registry.request_cancel(self.session_id, "parent")
        session.cancel()
        await asyncio.gather(session, return_exceptions=True)
        self._task = self._run = None
        cancellation_registry.consume(self.session_id)

    async def _drive(
        self, run: RunRecord, preceding_run: RunRecord | None
    ) -> None:
        """驱动一个 Run 并释放执行，再交给 ``on_released``；失败由 ``RunDriver`` 落定。

        注销执行与取走信号之间没有 await（与 :meth:`cancel` 互斥），这是信号的唯一清理点。
        驱动异常退出（``RunDriver`` 落定自身失败、装配期读库失败）同样走这条释放路径：否则
        取消信号会残留到下一次执行，而剩余消息也不会被接续。进程关闭的取消由 :meth:`abort`
        负责收尾，不在这里跑释放回调。
        """
        # 每次驱动现构造：``RunDriver`` 一次构造对应一次驱动；也不能放进构造函数——space 在快照读取
        # 等许多路径上物化，构造执行登记不应牵出 Run / Session service 与存储的装配。
        try:
            await RunDriver().drive(run, preceding_run)
        except asyncio.CancelledError:
            # 进程关闭：``abort`` 自己补取信号并注销，不调度后续 Run。
            raise
        except Exception:
            # 后台 task 无人 await：这条日志是异常唯一的落点。释放边界照常走完。
            log.exception(
                "session_drive_failed",
                extra={
                    "msg": "Run 驱动异常退出，执行将由释放边界收尾",
                    "data": {"session_id": self.session_id, "run_id": run.id},
                },
            )
        self._task = self._run = None
        reason = cancellation_registry.consume(self.session_id)
        await self._on_released(run, reason)

    def _on_drive_done(self, done: asyncio.Task[None]) -> None:
        """执行结束的兜底：异常退出在这里落一条日志，不留无人取回失败的 session。"""
        if done.cancelled():
            return
        error = done.exception()
        if error is None:
            return
        log.error(
            "session_drive_crashed",
            extra={
                "msg": "Run 释放后收尾异常退出",
                "data": {"session_id": self.session_id},
            },
            exc_info=error,
        )
