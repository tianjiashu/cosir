"""Task 的执行登记：至多一个后台执行，与 Task 取消信号的打出、取走构成闭环。

由该 Task 的 ``TaskActor`` 持有。actor 决定「什么时候开、开完怎么接续」，本类只负责「这一次执行」：
登记、驱动、释放，以及取消信号在执行边界上的生命周期。

信号的不变量只在这里成立：**有执行者才打信号，注销执行时一并取走信号**，两步各自之间没有 await。
:meth:`_signal` 判定 ``driving`` 与打信号是同步的，:meth:`_drive` 注销执行与 ``consume`` 也是同步的，
二者在事件循环上互斥——信号要么被这次执行的收尾取走，要么根本不打，不会残留到下一个执行（残留
信号会让下一个 Run 一启动就被取消）。没有执行者时取消该做什么（挂起 Run 的迁移）不归这里，由 actor
在 Task 闸门内处理。

打信号与取消这次执行的 Agent Team 合在 :meth:`cancel` 一步：``agent_team`` 工具在线程里阻塞到 Team
落定、中途不检查信号，Team 必须在发起停止那一刻取消，不能等 Run 落定。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable

from app.core.workflow.run_driver import RunDriver
from app.core.task_cancellation import TaskCancellation
from app.core.workflow.cancel.conversation_run_cancellation_registry import (
    CancelReason,
    cancellation_registry,
)
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.core.workflow.message_queue.task_message import TaskMessage
from app.models import ConversationRunRecord

OnReleased = Callable[[ConversationRunRecord, CancelReason | None], Awaitable[None]]
"""执行释放后的回调：收到这次 Run 与收尾时取走的取消原因（没收到取消为 ``None``）。"""


class TaskExecution:
    """一个 Task 的执行登记；方法只能在事件循环线程调用。"""

    def __init__(
            self,
            task_id: int,
            mailbox: RunMailbox,
            cancellation: TaskCancellation,
            on_released: OnReleased,
    ) -> None:
        self.task_id = task_id
        self._mailbox = mailbox
        self._cancellation = cancellation
        # 释放后的处理（传播、接续）是调度判定，归 actor；这里只在释放后交出去。
        self._on_released = on_released
        # 当前后台执行（驱动一个 Run 的子 task）及其 Run；没有执行时为 ``None``，注销时一起清空。
        self._task: asyncio.Task[None] | None = None
        self._run: ConversationRunRecord | None = None

    @property
    def driving(self) -> bool:
        """是否有执行者；同步判定，不读库——打取消信号必须与执行收尾之间没有 await。"""
        return self._task is not None and not self._task.done()

    @property
    def stopping(self) -> bool:
        """有执行者且已收到取消信号：这次执行即将作废，不应再往 mailbox 投递。"""
        return self.driving and cancellation_registry.is_cancelled(self.task_id)

    def start(self, run: ConversationRunRecord, initial: Iterable[TaskMessage]) -> None:
        """清空 mailbox、投递初始消息，再把 Run 交给后台执行；调用方持 Task 闸门。

        执行作为独立的子 task 运行，调用方不等它：调度 loop 要继续监听（运行中到达的消息才能及时
        投进 mailbox），闸门也不能被整个 Run 占住。

        异常:
            ValueError: 已有执行（调用方违约）。
        """
        if self.driving:
            raise ValueError(f"task {self.task_id} 已有执行")
        self._mailbox.reset()
        for message in initial:
            self._mailbox.post(message)
        self._run = run
        self._task = asyncio.get_running_loop().create_task(self._drive(run))

    async def cancel(self, reason: CancelReason) -> bool:
        """有执行者时打取消信号，并立即取消这次执行的 Run 运行中的 Team；返回是否打了信号。

        Team 按这次执行的 Run 寻址，不读「当前 Run」：打完信号之后执行可能已释放、actor 已开出新 Run，
        按当前 Run 取消会误伤新 Run 的 Team。没有执行者时什么也不做。
        """
        run = self._run
        if not self._signal(reason):
            return False
        await self._cancellation.cancel_running_teams(run.id)
        return True

    def _signal(self, reason: CancelReason) -> bool:
        """有执行者时打 Task 取消信号，返回是否打了；没有执行者时什么也不登记。"""
        if not self.driving:
            return False
        cancellation_registry.request_cancel(self.task_id, reason)
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
        task = self._task
        cancellation_registry.request_cancel(self.task_id, "parent")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._task = self._run = None
        cancellation_registry.consume(self.task_id)

    async def _drive(self, run: ConversationRunRecord) -> None:
        """驱动一个 Run 并释放执行，再交给 ``on_released``；失败由 ``RunDriver`` 落定。

        注销执行与取走信号之间没有 await（与 :meth:`_signal` 互斥），这是信号的唯一清理点。
        """
        # 每次驱动现构造：``RunDriver`` 一次构造对应一次驱动；也不能放进构造函数——space 在快照读取
        # 等许多路径上物化，构造执行登记不应牵出 Run / Task service 与存储的装配。
        await RunDriver().drive(run)
        if cancellation_registry.is_cancelled(self.task_id):
            # 取走信号前先撤销延迟结果：信号在时迟到结论被 ``deliver_background_result`` 丢弃，
            # 取走之后、撤销之前却会入队并开出新 Run。撤销期间执行仍登记着，新到的取消照常打信号。
            await self._cancellation.drop_deferred_entries()
        self._task = self._run = None
        reason = cancellation_registry.consume(self.task_id)
        await self._on_released(run, reason)
