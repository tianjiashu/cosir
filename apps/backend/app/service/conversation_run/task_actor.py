"""Task 级调度 actor：每个 Task 一个单消费者，串行执行推进、收尾、取消、恢复与编辑重跑。

设计见 ``docs/plan/task-actor-dispatch-design.md``。生产者（:class:`WorkflowSession` 的各入口）
只发信号；改变 Task 运行状态的动作全部在本 actor 的循环里按信号顺序执行，因此同一 Task 不会出现
两个执行者，也不需要「执行收尾窗口」的特殊等待。判定规则仍是 ``dispatch_policy`` 的纯函数。

两条约束决定了循环的形状：

- **唯一等待点是信号队列。** Run 作为子 asyncio task 运行，结束时由执行自身发 ``RunFinished``；
  actor 从不 ``await`` Run，Run 运行期间照常处理投递与取消。
- **处理函数只做短 IO。** 需要「等 Run 停下」的动作（停止、编辑重跑）拆成两阶段：先打取消信号
  并记下待办，由 ``RunFinished`` 收尾时完成。actor 之间只发信号、不等对方回执，避免父子 Task
  互等死锁。

每批信号在 Task 闸门（``TaskRuntimeSpace`` 操作锁）内处理：调度入口之间已由 actor 串行，这把锁
只剩与 Task 删除、fork 的互斥。actor 不跨重启，重启后按需拉起并重读事实源（外部队列与 Run 表）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.workflow.cancel.conversation_run_cancellation_registry import (
    CancelReason,
    cancellation_registry,
)
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.models import ConversationRunRecord
from app.models.enums.conversation_run_status import (
    TERMINAL_RUN_STATUSES,
    ConversationRunStatus,
    RunTransition,
)
from app.models.task_message import AgentResultBody, NoticeBody, TaskMessage, UserInputBody
from app.service import depends as service_depends
from app.service.conversation_run import dispatch_policy, message_edit
from app.service.conversation_run.run_driver import drive_run
from app.service.conversation_run.task_signals import (
    Cancel,
    DeliverPending,
    Enqueue,
    Rerun,
    Resume,
    RunFinished,
    SessionConflictError,
    SessionReceipt,
    Signal,
    Stop,
    abandon,
    reject,
    resolve,
)
from app.storage.store_engines import main_session_factory
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

if TYPE_CHECKING:
    from app.service.conversation_run.workflow_session import WorkflowSession

_USER_CANCELLED_NOTICE = (
    "The user stopped your previous turn before it finished. Do not resume that work unless "
    "the user asks for it."
)


def _is_suspended(run: ConversationRunRecord | None) -> bool:
    return run is not None and run.status == ConversationRunStatus.SUSPENDED.value


def _republish(task_id: int) -> None:
    """从 canonical 记录重建并发布快照：新开 Run 与编辑重跑都会改写已投影的历史。"""
    states = service_depends.get_conversation_task_state_service()
    states.publish_state(task_id, states.rebuild_state(task_id))


def _refresh_queued(task_id: int) -> None:
    service_depends.get_conversation_task_state_service().refresh_queued_inputs(task_id)


def _receipt(action: dispatch_policy.Action) -> SessionReceipt:
    """新建或唤醒了 Run 时 ``queued`` 为假；其余情况消息留在外部队列（运行中已投递给当前执行）。"""
    return SessionReceipt(
        queued=not isinstance(action, dispatch_policy.StartRun | dispatch_policy.Wake)
    )


@dataclass
class Execution:
    """一次后台执行：驱动单个 Run，随 ``RunFinished`` 收尾注销。"""

    run: ConversationRunRecord
    # 新建的 Run 初始投递全部剩余消息；续跑 / 唤醒只投递 immediate（见 dispatch_policy）。
    fresh_run: bool
    # 本次执行的内部消息队列；actor 投递，prepare_input 取光，随执行丢弃。
    mailbox: RunMailbox = field(default_factory=RunMailbox)
    # 启动它的那一批信号处理完才置位：初始投递必须排在本批的排队投影与快照发布之后，
    # prepare_input 的消费事件才总能在排队列表里找到条目。
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    # RunFinished 收尾（含交接）完成后置位，供跨 Run 订阅方等交接出的新 Run。
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    thread_task: asyncio.Task[None] = field(init=False)


class TaskActor:
    """一个 Task 的调度消费者；只能在事件循环线程上创建与发信号。"""

    def __init__(self, task_id: int, session: WorkflowSession) -> None:
        self.task_id = task_id
        self._session = session
        self._signals: asyncio.Queue[Signal] = asyncio.Queue()
        self.execution: Execution | None = None
        # 等当前 Run 停下后再完成的编辑重跑（两阶段的第二阶段，见 :meth:`_rerun`）。
        self._pending_rerun: Rerun | None = None
        self._stopping = False
        self._stop_waiters: list[asyncio.Future[Any]] = []
        # 静止 = 没有执行者且最新 Run 已到终态（挂起不算）；每批处理后重算并通知等待方。
        self.quiescent = False
        self.state_changed = asyncio.Event()
        self._runs = service_depends.get_conversation_run_service()
        self._run_state = service_depends.get_conversation_run_state_service()
        self._tasks = service_depends.get_task_service()
        self._queue = service_depends.get_task_message_queue()
        self.loop_task: asyncio.Task[None] = asyncio.create_task(self._loop())

    def send(self, signal: Signal) -> None:
        self._signals.put_nowait(signal)

    def signal_execution(self, reason: CancelReason) -> bool:
        """向当前执行打 Task 取消信号；没有执行时返回 ``False``。

        入口在发信号前同步调用：运行中的取消不排在其他信号之后，检查点立即可见。与
        :meth:`_finish` 的「注销 + 取走信号」同在事件循环线程上、之间没有 await，二者互斥。
        """
        if self.execution is None:
            return False
        cancellation_registry.request_cancel(self.task_id, reason)
        return True

    def abandon(self) -> None:
        """进程关闭：以取消兑现所有未决回执，避免调用方永久挂起。"""
        while not self._signals.empty():
            abandon(self._signals.get_nowait())
        if self._pending_rerun is not None:
            abandon(self._pending_rerun)
            self._pending_rerun = None
        for waiter in self._stop_waiters:
            if not waiter.done():
                waiter.cancel()
        self._stop_waiters.clear()

    async def _loop(self) -> None:
        while True:
            batch = [await self._signals.get()]
            while not self._signals.empty():
                batch.append(self._signals.get_nowait())
            try:
                async with task_runtime_spaces.get_or_create(self.task_id).async_operation():
                    await self._handle(batch)
            finally:
                # 本批启动的执行此刻才放行初始投递；处理异常时也必须放行，否则 drive_run 永久等待。
                if self.execution is not None:
                    self.execution.ready.set()
            await self._publish_state()
            if self._stopping and self.execution is None and self._signals.empty():
                # 判空与注销之间没有 await：之后到达的信号会拉起新的 actor，不会丢。
                self._session.evict(self)
                for waiter in self._stop_waiters:
                    resolve(waiter, None)
                self._stop_waiters.clear()
                return

    async def _handle(self, batch: list[Signal]) -> None:
        """逐个处理；相邻的入队信号合并成一次推进，统一兑现回执。

        单个信号失败只拒绝它自己的回执并记录，不影响同批其他信号，也不杀死循环。
        """
        merged: list[Enqueue] = []
        for signal in batch:
            if isinstance(signal, Enqueue):
                await self._guard(signal, partial(self._enqueue, signal, merged))
                continue
            await self._flush(merged)
            merged = []
            await self._guard(signal, partial(self._dispatch, signal))
        await self._flush(merged)

    async def _guard(self, signal: Signal, handler: Callable[[], Awaitable[None]]) -> None:
        try:
            await handler()
        except Exception as exc:
            log.exception(
                "task_actor_signal_failed",
                extra={
                    "msg": "Task actor 处理信号失败",
                    "data": {"task_id": self.task_id, "signal": type(signal).__name__},
                },
            )
            reject(signal, exc)

    async def _dispatch(self, signal: Signal) -> None:
        if isinstance(signal, RunFinished):
            await self._finish(signal.execution)
        elif isinstance(signal, Cancel):
            resolve(signal.receipt, await self._cancel(signal.reason))
        elif isinstance(signal, Stop):
            self._stopping = True
            await self._cancel("parent")
            if signal.receipt is not None:
                self._stop_waiters.append(signal.receipt)
        elif isinstance(signal, Resume):
            resolve(signal.receipt, await self._resume())
        elif isinstance(signal, Rerun):
            await self._rerun(signal)
        elif isinstance(signal, DeliverPending):
            await self._deliver_pending()
        else:
            raise TypeError(f"未知信号 {type(signal).__name__}")

    async def _enqueue(self, signal: Enqueue, merged: list[Enqueue]) -> None:
        """入外部队列；系统消息先按 ``dispatch_policy.admit`` 过滤（用户取消后丢弃下游结果）。"""
        if signal.admit:
            latest = await asyncio.to_thread(self._tasks.require_latest_run, self.task_id)
            if not dispatch_policy.admit(signal.message.body, latest):
                log.info(
                    "task_message_dropped_after_cancel",
                    extra={
                        "msg": "父 Task 已取消，丢弃被停止下游的结果消息",
                        "data": {"task_id": self.task_id, "kind": signal.message.body.kind},
                    },
                )
                resolve(signal.receipt, SessionReceipt(queued=True))
                return
        await asyncio.to_thread(self._queue.put, self.task_id, signal.message)
        merged.append(signal)

    async def _flush(self, merged: list[Enqueue]) -> None:
        if not merged:
            return
        try:
            action = await self._advance(
                by_user=any(item.by_user for item in merged),
                project=any(item.message.project_to_transport for item in merged),
            )
        except Exception as exc:
            log.exception(
                "task_actor_advance_failed",
                extra={
                    "msg": "入队后推进失败，消息留在外部队列",
                    "data": {"task_id": self.task_id},
                },
            )
            for item in merged:
                reject(item, exc)
            return
        for item in merged:
            resolve(item.receipt, _receipt(action))

    async def _advance(self, *, by_user: bool, project: bool = False) -> dispatch_policy.Action:
        """按 ``dispatch_policy.next_action`` 推进并返回判定：投递、唤醒、交接新 Run 或不动。

        ``by_user`` 表示这次推进由用户发起（发送、编辑），只有它能从取消、失败后交接新 Run。
        停止中（Task 即将删除）与编辑重跑等待中不推进：消息留在外部队列，重跑完成后一并并入新 Run。
        ``project`` 为真时先刷新排队投影再投递；新建 Run 由随后的快照重建覆盖。新建后先登记执行再
        发布快照——顺序反过来时，一次投影异常就会留下「running 但没有执行者」的 Task；快照发布属于
        观测旁路，失败只记录。
        """
        if self._stopping or self._pending_rerun is not None:
            if project:
                await asyncio.to_thread(_refresh_queued, self.task_id)
            return dispatch_policy.Stay()
        latest = await asyncio.to_thread(self._tasks.require_latest_run, self.task_id)
        pending = await asyncio.to_thread(self._queue.pending, self.task_id)
        action = dispatch_policy.next_action(
            latest, self.execution is not None, pending, by_user=by_user
        )
        if project and not isinstance(action, dispatch_policy.StartRun):
            await asyncio.to_thread(_refresh_queued, self.task_id)
        if isinstance(action, dispatch_policy.Deliver):
            self._post(action.messages)
        elif isinstance(action, dispatch_policy.Wake):
            woken = cast(ConversationRunRecord, latest)
            await self._transition_and_start(woken, RunTransition.WAKE)
        elif isinstance(action, dispatch_policy.StartRun):
            run = await asyncio.to_thread(self._runs.create_run, self.task_id, action.launch)
            self._start(run, fresh_run=True)
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
        return action

    async def _finish(self, execution: Execution) -> None:
        """执行结束：注销并取走 Task 取消信号（信号的唯一清理点），按 Run 落定状态继续调度。"""
        self.execution = None
        reason = cancellation_registry.consume(self.task_id)
        try:
            await self._after_run(execution.run, reason)
        finally:
            execution.settled.set()
        rerun, self._pending_rerun = self._pending_rerun, None
        if rerun is not None:
            await self._guard(rerun, partial(self._apply_rerun, rerun))

    async def _after_run(self, run: ConversationRunRecord, reason: CancelReason | None) -> None:
        """按 Run 落定的状态与注销瞬间的取消信号继续调度。

        挂起后推进即防丢唤醒：挂起前后到达的可唤醒消息此刻都已在外部队列里。``reason`` 非空说明
        最后一个检查点之后才到了取消：挂起已落定的直接迁移为 cancelled，完成的不再交接。
        """
        try:
            current = await asyncio.to_thread(self._runs.get_run, run.id)
        except KeyError:
            # 终态已落库后允许删除历史，执行者随后照常释放。
            log.info(
                "run_released_after_deletion",
                extra={"msg": "已删除 Run 的执行者完成释放", "data": {"run_id": run.id}},
            )
            return
        if current.status == ConversationRunStatus.SUSPENDED.value:
            if reason is None:
                await self._advance(by_user=False)
            else:
                await self._cancel_suspended(current, reason)
            return
        if current.status == ConversationRunStatus.COMPLETED.value:
            if reason is None:
                await self._advance(by_user=False)
            else:
                log.info(
                    "run_handoff_skipped_by_cancel",
                    extra={
                        "msg": "Run 完成时已收到停止请求，不为排队消息接续新 Run",
                        "data": {"run_id": run.id, "task_id": self.task_id},
                    },
                )
            await self._report_to_parent(current, reason)
        elif current.status == ConversationRunStatus.CANCELLED.value:
            await self._after_cancelled(current, reason or "user")
        elif current.status == ConversationRunStatus.FAILED.value:
            await self._report_to_parent(current, reason)

    async def _cancel(self, reason: CancelReason) -> bool:
        """停止当前执行或挂起的 Run；没有可停止的对象时返回 ``False``。

        有执行时只打信号，由检查点落定、随 ``RunFinished`` 收尾；挂起中没有执行者，直接迁移。
        """
        if self.signal_execution(reason):
            return True
        latest = await asyncio.to_thread(self._tasks.require_latest_run, self.task_id)
        return _is_suspended(latest) and await self._cancel_suspended(
            cast(ConversationRunRecord, latest), reason
        )

    async def _cancel_suspended(self, run: ConversationRunRecord, reason: CancelReason) -> bool:
        """挂起的 Run 没有执行者，直接迁移为 cancelled 并收尾。

        迁移落空（状态已被并发改变）时返回 ``False``，不收尾。
        """
        cancelled = await asyncio.to_thread(
            self._run_state.transition,
            run.id,
            RunTransition.CANCEL,
            end_reason=Constant.Run.RUN_END_REASON_RUNTIME_CANCELLED,
        )
        if cancelled is None:
            return False
        await self._after_cancelled(cancelled, reason)
        return True

    async def _after_cancelled(self, run: ConversationRunRecord, reason: CancelReason) -> None:
        """Run 落定 cancelled 后：向下游传播，用户取消再留一条「用户已取消」通知。

        通知以 ``next_run`` 入队、不唤醒，随用户下一次发送新建的 Run 并入；运行中编辑与上游停止不留
        通知（编辑会移出被打断的内容，上游停止由上游自行处理）。传播失败只记录，不影响本 Run 终态。
        """
        await self._propagate_cancel(run)
        await self._report_to_parent(run, reason)
        if reason != "user":
            return
        notice = TaskMessage.of(
            NoticeBody(content=_USER_CANCELLED_NOTICE, notice_kind="user_cancelled"),
            delivery="next_run",
        )
        await asyncio.to_thread(self._queue.put, self.task_id, notice)

    async def _report_to_parent(
            self, run: ConversationRunRecord, reason: CancelReason | None
    ) -> None:
        """委派子 Task 的一次 Run 到终态后，把结论投递给父 Task，唤醒挂起或已完成的父 Agent。

        只有 ``delegate_task`` 子 Task 回投（Team 节点 Task 也挂在主 Task 下，但结论由 Coordinator
        收集，回投会让每个节点都唤醒主 Agent）；因父 Agent 停止而被取消的子 Run（``parent``）不
        回投，父 Agent 已被用户取消时由父 actor 的入队过滤丢弃。只发信号、不等父 actor 回执。
        """
        if reason == "parent":
            return
        task = await asyncio.to_thread(self._tasks.get_task, run.task_id)
        if task.task_type != "delegate_task" or task.parent_task_id is None:
            return
        body = AgentResultBody(
            child_task_id=run.task_id,
            status=run.status,
            final_output=run.final_output,
            end_reason=run.end_reason,
        )
        self._session.post_nowait(task.parent_task_id, body)

    async def _propagate_cancel(self, run: ConversationRunRecord) -> None:
        """向下游发出取消：Agent Team（待确认与运行中）与子 Agent 都按上游停止处理。"""
        from app.agent_team.coordinator import get_agent_team_coordinator

        try:
            await asyncio.to_thread(get_agent_team_coordinator().cancel_for_parent_run_id, run.id)
        except Exception:
            log.exception(
                "agent_team_parent_cancel_propagation_failed",
                extra={"msg": "主 Agent 取消后传播到 Agent Team 失败", "data": {"run_id": run.id}},
            )
        children = await asyncio.to_thread(self._tasks.list_child_tasks, run.task_id)
        for child in children:
            self._session.cancel_nowait(child.id, "parent")

    async def _resume(self) -> SessionReceipt:
        latest = await asyncio.to_thread(self._tasks.require_latest_run, self.task_id)
        resumed = (
            await self._transition_and_start(latest, RunTransition.RESUME)
            if latest is not None and self.execution is None
            else None
        )
        if resumed is None:
            raise SessionConflictError("RUN_NOT_RESUMABLE", "当前运行不可恢复")
        return SessionReceipt(queued=False)

    async def _rerun(self, signal: Rerun) -> None:
        """编辑重跑第一阶段：运行中先以 ``edited`` 停止并记下待办，挂起中直接迁移为 cancelled。"""
        if self._pending_rerun is not None:
            raise SessionConflictError(
                "RUN_NOT_STOPPED", "上一次编辑仍在停止当前运行，请稍后重试"
            )
        if self.signal_execution("edited"):
            self._pending_rerun = signal
            return
        latest = await asyncio.to_thread(self._tasks.require_latest_run, self.task_id)
        if _is_suspended(latest):
            await self._cancel_suspended(cast(ConversationRunRecord, latest), "edited")
        await self._apply_rerun(signal)

    async def _apply_rerun(self, signal: Rerun) -> None:
        """编辑重跑第二阶段：移出目标及其后 context 与新消息入队同一事务，随后推进新 Run。

        停止期间被打断的 Run 可能又并入了后续输入；目标不再是最新一条时拒绝，避免把这些输入
        一并移出。
        """
        latest = await asyncio.to_thread(self._tasks.require_latest_run, self.task_id)
        if latest is not None and latest.status not in TERMINAL_RUN_STATUSES:
            # 编辑只改已停下的 Task：停止落空（迁移被并发抢先）时交给用户重试，
            # 不改写活动 Run 的 context。
            raise SessionConflictError("RUN_NOT_STOPPED", "当前运行尚未停止，请稍后重试")
        target = await asyncio.to_thread(message_edit.latest_user_input, self.task_id)
        if target is None or target[0] != signal.message_id:
            raise ValueError("只能编辑最新用户消息")
        await asyncio.to_thread(self._exclude_and_put, signal.message_id, signal.body)
        resolve(signal.receipt, _receipt(await self._advance(by_user=True)))

    def _exclude_and_put(self, message_id: int, body: UserInputBody) -> None:
        with main_session_factory().begin() as session:
            message_edit.exclude_from(session, self.task_id, message_id)
            self._queue.put(self.task_id, TaskMessage.of(body), session=session)

    async def _deliver_pending(self) -> None:
        """配置广播：只投递给正在执行的 Run，不推进、不启动 Run。"""
        if self.execution is None:
            return
        pending = await asyncio.to_thread(self._queue.pending, self.task_id)
        self._post([message for message in pending if message.delivery == "immediate"])

    async def _transition_and_start(
            self, run: ConversationRunRecord, transition: RunTransition
    ) -> ConversationRunRecord | None:
        """条件迁移已停下的 Run 并启动执行；迁移落空（状态已变）时返回 ``None``，不启动。"""
        resumed = await asyncio.to_thread(self._run_state.transition, run.id, transition)
        if resumed is not None:
            self._start(resumed, fresh_run=False)
        return resumed

    def _start(self, run: ConversationRunRecord, *, fresh_run: bool) -> None:
        """登记并创建后台执行；HTTP 断开不会取消它。

        异常:
            ValueError: 该 Task 已有执行（调用方违约）。
        """
        if self.execution is not None:
            raise ValueError(f"task {self.task_id} 已有执行")
        execution = Execution(run=run, fresh_run=fresh_run)
        execution.thread_task = asyncio.create_task(self._execute(execution))
        self.execution = execution

    async def _execute(self, execution: Execution) -> None:
        # 后台 task 无人 await：异常在这里记录，否则会被 asyncio 静默吞掉。关闭期的
        # CancelledError 照常上浮，不再发 RunFinished——关闭不调度后续 Run。
        try:
            await drive_run(
                execution.run.id, execution.mailbox, lambda: self._initial_delivery(execution)
            )
        except Exception:
            log.exception(
                "conversation_run_execution_failed",
                extra={"msg": "Run 驱动以异常结束", "data": {"run_id": execution.run.id}},
            )
        self.send(RunFinished(execution))

    async def _initial_delivery(self, execution: Execution) -> None:
        """执行的初始投递：新 Run 的输入、恢复时积压的输入与通知都经它进入 mailbox。

        交给 drive_run 在其失败边界内执行，读库失败也落定 failed。
        """
        await execution.ready.wait()
        pending = await asyncio.to_thread(self._queue.pending, self.task_id)
        for message in dispatch_policy.initial_messages(pending, fresh_run=execution.fresh_run):
            execution.mailbox.post(message)

    def _post(self, messages: list[TaskMessage]) -> None:
        """投进当前执行的 mailbox；重复由 mailbox 按行 id 去重，投递不删行。"""
        if self.execution is None:
            return
        for message in messages:
            self.execution.mailbox.post(message)

    async def _publish_state(self) -> None:
        """重算静止并唤醒 ``wait_idle`` 等待方；观测旁路，失败只记录。"""
        try:
            latest = await asyncio.to_thread(self._tasks.require_latest_run, self.task_id)
        except KeyError:
            latest = None
        except Exception:
            log.exception(
                "task_actor_state_publish_failed",
                extra={"msg": "Task actor 重算静止状态失败", "data": {"task_id": self.task_id}},
            )
            return
        self.quiescent = self.execution is None and (
            latest is None or latest.status in TERMINAL_RUN_STATUSES
        )
        changed, self.state_changed = self.state_changed, asyncio.Event()
        changed.set()
