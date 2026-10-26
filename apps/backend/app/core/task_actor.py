"""Task 级调度者：每个 Task 一个，常驻监听外部队列，按 Run 状态投递、唤醒或开新 Run。

actor 随 ``TaskRuntimeSpace`` 物化而创建，由注册表在事件循环上拉起，常驻到 Task 删除或进程
关闭。它不是消息事实源：消息在外部队列（``task_messages``），取消意图在
``cancellation_registry``，Run 状态在 Run 表；每次醒来都重读事实源，再交给
``dispatch_policy`` 判定。

几条约束决定了它的形状：

- **只等外部队列，按水位等新消息。** 投递不删行，``prepare_input`` 写 context 时才确认删除；若按
  「队列非空」醒来，尚未被消费的行会让 loop 空转、反复投递。水位之后的消息才算新到达，只有它们
  触发推进；残留消息随下一次推进一并投递。
- **执行不阻塞 loop。** Run 作为子 asyncio task 运行，loop 继续监听：运行中到达的 ``immediate``
  消息能及时投进 Task 级 mailbox。执行结束时若队列里还有未消费的消息，按刚结束的 Run 状态再推进
  一次（完成、挂起才接续；取消、失败不自动接续——用户停止的不再自己跑起来，持续失败也不会空转）。
- **「判定 + 启动执行」在 Task 闸门内串行。** loop、执行结束后的接续与 HTTP 的恢复命令都在闸门内
  读 Run 状态再启动，同一 Task 不会出现两个执行者；闸门同时与删除、fork 互斥。执行期间不持闸门。
- **取消不经 loop。** 运行中的取消由入口同步打 ``cancellation_registry`` 信号，工作流在检查点落定。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, cast

from app.config.logging.logger import log
from app.core.run_driver import RunDriver
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.core.workflow.inbox import dispatch_policy
from app.core.workflow.inbox.run_mailbox import RunMailbox
from app.core.workflow.inbox.task_message import TaskMessage
from app.models import ConversationRunRecord
from app.models.enums.conversation_run_status import (
    TERMINAL_RUN_STATUSES,
    ConversationRunStatus,
    RunTransition,
)
from app.service import depends as service_depends

if TYPE_CHECKING:
    from app.task_runtime.task_runtime_space import TaskRuntimeSpace

# 执行结束后自动接续剩余消息的 Run 落定状态：取消是用户的停止，失败可能持续失败，都等新消息。
_CONTINUE_AFTER_STATUSES = frozenset(
    {ConversationRunStatus.COMPLETED.value, ConversationRunStatus.SUSPENDED.value}
)


class SessionConflictError(ValueError):
    """当前 Task 状态不允许本次用户命令；``code`` 是稳定的机器码，API 映射为 409。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _republish(task_id: int) -> None:
    """从 canonical 记录重建并发布快照：新开 Run 会改写已投影的历史。"""
    states = service_depends.get_conversation_task_state_service()
    states.publish_state(task_id, states.rebuild_state(task_id))


class TaskActor:
    """一个 Task 的调度者；``start`` / ``stop`` 等方法只能在事件循环线程调用。"""

    def __init__(self, space: TaskRuntimeSpace) -> None:
        self.task_id = space.task_id
        self._space = space
        # Task 级投递通道：每次开始执行时清空，运行中 loop 往里投递，prepare_input 取光。
        self.mailbox = RunMailbox()
        # 当前后台执行（驱动一个 Run 的子 task）；没有执行或已结束时为 ``None``。
        self.execution: asyncio.Task[None] | None = None
        self._loop_task: asyncio.Task[None] | None = None
        # 已判定过的最大消息 id：之后到达的才算新消息。
        # 外部队列「取出不删除」：行要等 prepare_input 写进 context 时才确认删除
        self._watermark = 0
        self._stopping = False
        # loop 停下后置位：删除流程等它再删，进程关闭时也置位。
        self.stopped = asyncio.Event()
        # 静止 = 没有执行者且当前 Run 已到终态（挂起不算）；每次判定、执行结束后重算。
        self.quiescent = False
        self.state_changed = asyncio.Event()

    # 协作者按需取进程单例，不在构造时取：space 在快照读取等许多路径上物化，构造 actor 不应
    # 牵出整套 Run / Task service 的装配。
    @property
    def _ledger(self):
        return service_depends.get_conversation_run_ledger()

    @property
    def _tasks(self):
        return service_depends.get_task_service()

    @property
    def _queue(self):
        return service_depends.get_task_message_queue()

    # ── 生命周期 ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """在当前事件循环上拉起监听 loop；重复调用无效果。"""
        if self._loop_task is None:
            self._loop_task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        """Task 即将删除：停止当前执行并等它落定，再停下 loop。不调度后续 Run。"""
        self._stopping = True
        execution = self.execution
        if execution is not None and not execution.done():
            cancellation_registry.request_cancel(self.task_id, "parent")
            await asyncio.wait({execution})
        await self._cancel_loop()

    async def abort(self) -> None:
        """进程关闭：直接取消执行与 loop，不等工作流落定；遗留 running Run 由下次启动收敛。"""
        self._stopping = True
        execution = self.execution
        if execution is not None and not execution.done():
            cancellation_registry.request_cancel(self.task_id, "parent")
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        await self._cancel_loop()

    async def _cancel_loop(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        self.stopped.set()

    # ── 监听与推进 ────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while True:
            #pending：外部队列里这个 Task 所有还没确认删除的消息，包括之前已经判定过、但还没被 prepare_input 消费的。它回答的是「现在队列里有什么」。
            pending = await self._queue.pending(self.task_id, self._watermark)
            #fresh：pending 里 id 大于水位的那部分，也就是这次新到达、还没判定过的消息。它回答的是「这次是什么把我叫醒的」。
            fresh = [message for message in pending if cast(int, message.id) > self._watermark]
            self._watermark = max(cast(int, message.id) for message in pending)
            try:
                await self._advance(pending, fresh)
            except Exception:
                # 单轮失败只记录，不杀死 loop：消息仍在外部队列，下一次推进重读事实源。
                log.exception(
                    "task_actor_advance_failed",
                    extra={"msg": "Task 调度推进失败", "data": {"task_id": self.task_id}},
                )

    async def _advance(self, pending: list[TaskMessage], fresh: list[TaskMessage]) -> None:
        """在 Task 闸门内按 ``dispatch_policy`` 推进一次：投递、唤醒、新开 Run 或不动。

        新建 Run 后先登记执行再发布快照：顺序反过来时，一次投影异常就会留下「running 但没有执行者」
        的 Task；快照发布属于观测旁路，失败只记录。
        """
        if self._stopping:
            return
        async with self._space.async_operation():
            current = await asyncio.to_thread(self._tasks.require_current_run, self.task_id)
            action = dispatch_policy.next_action(current, self._running(), pending, fresh)
            if isinstance(action, dispatch_policy.Deliver):
                for message in action.messages:
                    self.mailbox.post(message)
            elif isinstance(action, dispatch_policy.Wake):
                woken = await asyncio.to_thread(
                    self._ledger.transition, cast(ConversationRunRecord, current).id,
                    RunTransition.WAKE,
                )
                if woken is not None:
                    self._start(woken, pending, fresh_run=False)
            elif isinstance(action, dispatch_policy.StartRun):
                run = await asyncio.to_thread(self._ledger.create_run, self.task_id, action.launch)
                self._start(run, pending, fresh_run=True)
                try:
                    await asyncio.to_thread(_republish, self.task_id)
                except Exception as exc:
                    log.exception(
                        "run_snapshot_publish_failed",
                        extra={
                            "msg": "新 Run 已启动，但快照发布失败；等后续事件或重建收敛",
                            "data": {"task_id": self.task_id, "run_id": run.id},
                        },
                        exc_info=exc,
                    )
        await self._publish_state()

    async def resume(self) -> None:
        """当前 Run 为 cancelled / failed 且没有执行者时，从 prepare_input 续跑同一个 Run。

        不经 loop（恢复时通常没有新消息、loop 正挂起），但同样在 Task 闸门内判定与启动：用户恰好
        同时发了消息时，两边不会各开一个执行。

        异常:
            SessionConflictError: ``RUN_NOT_RESUMABLE``。
            KeyError: Task 不存在。
        """
        async with self._space.async_operation():
            current = await asyncio.to_thread(self._tasks.require_current_run, self.task_id)
            resumed = (
                await asyncio.to_thread(self._ledger.transition, current.id, RunTransition.RESUME)
                if current is not None and not self._running()
                else None
            )
            if resumed is None:
                raise SessionConflictError("RUN_NOT_RESUMABLE", "当前运行不可恢复")
            pending = await asyncio.to_thread(self._queue.pending_nowait, self.task_id)
            self._start(resumed, pending, fresh_run=False)
        await self._publish_state()

    async def wait_idle(self, wait_seconds: float) -> bool:
        """等待 Task 静止，最多 ``wait_seconds`` 秒；返回是否已静止（挂起不算静止）。"""
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while not self.quiescent:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self.state_changed.wait(), remaining)
            except TimeoutError:
                return False
        return True

    # ── 执行 ──────────────────────────────────────────────────────────────

    def _running(self) -> bool:
        return self.execution is not None and not self.execution.done()

    def _start(
            self, run: ConversationRunRecord, pending: list[TaskMessage], *, fresh_run: bool
    ) -> None:
        """清空 mailbox、投递初始消息，再把 Run 交给后台执行；调用方持 Task 闸门。

        异常:
            ValueError: 该 Task 已有执行（调用方违约）。
        """
        if self._running():
            raise ValueError(f"task {self.task_id} 已有执行")
        self.mailbox.reset()
        for message in dispatch_policy.initial_messages(pending, fresh_run=fresh_run):
            self.mailbox.post(message)
        self.quiescent = False
        self.execution = asyncio.get_running_loop().create_task(self._drive(run))

    async def _drive(self, run: ConversationRunRecord) -> None:
        """驱动一个 Run；失败由 ``RunDriver`` 落定。结束后按落定状态决定是否接续剩余消息。"""
        await RunDriver(self.mailbox).drive(run)
        self.execution = None
        if self._stopping:
            return
        try:
            settled = await asyncio.to_thread(self._ledger.get_run, run.id)
            if settled.status in _CONTINUE_AFTER_STATUSES:
                pending = await asyncio.to_thread(self._queue.pending_nowait, self.task_id)
                if pending:
                    # 剩余消息都没被这个 Run 消费：当作新到达的消息再判定一次。
                    await self._advance(pending, pending)
                    return
        except Exception:
            log.exception(
                "task_actor_continue_failed",
                extra={
                    "msg": "Run 结束后接续剩余消息失败",
                    "data": {"task_id": self.task_id, "run_id": run.id},
                },
            )
        await self._publish_state()

    async def _publish_state(self) -> None:
        """重算静止并唤醒 ``wait_idle`` 等待方；观测旁路，失败只记录。"""
        try:
            current = await asyncio.to_thread(self._tasks.require_current_run, self.task_id)
        except KeyError:
            current = None
        except Exception:
            log.exception(
                "task_actor_state_publish_failed",
                extra={"msg": "Task actor 重算静止状态失败", "data": {"task_id": self.task_id}},
            )
            return
        self.quiescent = not self._running() and (
                current is None or current.status in TERMINAL_RUN_STATUSES
        )
        changed, self.state_changed = self.state_changed, asyncio.Event()
        changed.set()
