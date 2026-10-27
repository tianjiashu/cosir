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
  一次：完成、挂起时接续全部剩余消息；取消、失败不自动接续，持续失败也不会空转。
- **停止期间先等执行落定再判定。** 已发起停止时 loop 醒来不往即将作废的 mailbox 投递，而是等这次
  执行（含收尾）结束，再按 ``idle`` 照常判定：用户停下后又发的消息就是这一轮的新消息，自然开新
  Run；停止前排着的不是新消息，不自己跑起来。
- **「判定 + 启动执行」在 Task 闸门内串行。** loop、执行结束后的接续与 HTTP 的恢复命令都在闸门内
  读 Run 状态再启动，同一 Task 不会出现两个执行者；闸门同时与删除、fork 互斥。执行期间不持闸门。
- **执行阶段单一判定。** Run 状态（持久化业务事实）与执行登记（进程内是否有执行者）只在
  ``dispatch_policy.phase_of`` 合成一个阶段（driving / suspended / idle），调度、取消、恢复、
  静止与 HTTP 侧的活动判断都只看阶段，不各自组合两者：执行者的释放滞后于 Run 落定。
- **取消归 actor。** :meth:`cancel` 看得到执行登记：有执行者就同步打信号（判定与打信号之间没有
  await，不会与收尾交错），并立即取消本 Run 的 Agent Team（工具阻塞在 Team 上时到不了检查点），
  工作流在检查点落定；挂起的 Run 没有执行者，在闸门内直接迁移为
  cancelled；没有可停止的对象时什么也不登记——没有执行者时登记的信号没人取走，会杀死下一个 Run。
  信号的唯一清理点是执行收尾（:meth:`_drive`），actor 由此知道取消原因：向下游传播（Agent Team、
  子 Task 以 ``parent`` 原因停止），用户取消再给下一个 Run 留一条通知。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, cast

from app.config.logging.logger import log
from app.core.run_driver import RunDriver
from app.core.tool.schemas.tool_names import TOOL_DELEGATE_TASK
from app.core.workflow.cancel.conversation_run_cancellation_registry import (
    CancelReason,
    cancellation_registry,
)
from app.core.workflow.inbox import dispatch_policy
from app.core.workflow.inbox.run_mailbox import RunMailbox
from app.core.workflow.inbox.task_message import BackgroundResultBody, NoticeBody, TaskMessage
from app.models import ConversationRunRecord
from app.models.enums.conversation_run_status import (
    TERMINAL_RUN_STATUSES,
    ConversationRunStatus,
    RunTransition,
)
from app.service import depends as service_depends

if TYPE_CHECKING:
    from app.task_runtime.task_runtime_space import TaskRuntimeSpace

# 执行结束后自动接续剩余消息的 Run 落定状态（且未收到取消）；停止之后到达的消息由 loop 判定。
_CONTINUE_AFTER_STATUSES = frozenset(
    {ConversationRunStatus.COMPLETED.value, ConversationRunStatus.SUSPENDED.value}
)

_USER_CANCELLED_NOTICE = (
    "The user stopped your previous turn before it finished. Do not resume that work unless "
    "the user asks for it."
)


class SessionConflictError(ValueError):
    """当前 Task 状态不允许本次用户命令；``code`` 是稳定的机器码，API 映射为 409。

    ``retryable`` 表示「刷新状态后重试可能成功」，前端据此决定是否提供重新同步入口；冲突已成定局
    （如排队消息已被并入）时为 ``False``。
    """

    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _refresh_queued(task_id: int) -> None:
    service_depends.get_conversation_task_state_service().refresh_queued_messages(task_id)


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
        # 阶段可能变化时换新并置位，``wait_idle`` 据此重新判定。
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
        if self._signal("parent"):
            await self._cancel_teams()
            await asyncio.wait({cast(asyncio.Task[None], execution)})
        await self._cancel_loop()

    async def abort(self) -> None:
        """进程关闭：直接取消执行与 loop，不等工作流落定；遗留 running Run 由下次启动收敛。"""
        self._stopping = True
        execution = self.execution
        if execution is not None and not execution.done():
            cancellation_registry.request_cancel(self.task_id, "parent")
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            # 执行被直接取消，收尾没有运行：在这里取走信号。
            cancellation_registry.consume(self.task_id)
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
            execution = self.execution
            if (
                    execution is not None
                    and not execution.done()
                    and cancellation_registry.is_cancelled(self.task_id)
            ):
                # 停止中：等执行与收尾结束再判定。用 ``asyncio.wait`` 而不是直接 await，loop 被取消
                # 时不连带取消执行。
                await asyncio.wait({execution})
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
        if fresh:
            # 先刷新排队投影再投递：消费投影只搬移排队条目，消息进 mailbox 前投影里必须已有它。
            try:
                await asyncio.to_thread(_refresh_queued, self.task_id)
            except Exception as exc:
                log.exception(
                    "queued_projection_refresh_failed",
                    extra={
                        "msg": "排队投影刷新失败，照常推进；等后续刷新或重建收敛",
                        "data": {"task_id": self.task_id},
                    },
                    exc_info=exc,
                )
        async with self._space.async_operation():
            current = await asyncio.to_thread(self._tasks.require_current_run, self.task_id)
            action = dispatch_policy.next_action(self._phase(current), current, pending, fresh)
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
        self._publish_state()

    async def cancel(self, reason: CancelReason = "user") -> None:
        """停止当前执行或挂起的 Run；没有可停止的对象时什么也不做。只发起停止，不等执行落定。

        有执行者时打信号并立即取消本 Run 的 Team，由工作流在检查点落定、收尾时传播与接续；挂起的
        Run 没有执行者，在闸门内直接迁移为 cancelled 并传播。重复取消幂等。

        异常:
            KeyError: Task 不存在。
        """
        if self._signal(reason):
            await self._cancel_teams()
            return
        async with self._space.async_operation():
            # 拿闸门期间可能已开出新执行：重新判定，不让信号落空或残留。
            signalled = self._signal(reason)
            if not signalled:
                current = await asyncio.to_thread(self._tasks.require_current_run, self.task_id)
                if self._phase(current) != "suspended":
                    return
                await self._cancel_teams()
                cancelled = await asyncio.to_thread(
                    self._ledger.transition, current.id, RunTransition.CANCEL
                )
                if cancelled is not None:
                    await self._after_cancelled(cancelled, reason)
                    await self._report_to_parent(cancelled, reason)
        if signalled:
            await self._cancel_teams()
        self._publish_state()

    async def _cancel_teams(self) -> None:
        """取消当前 Run 创建的待确认与运行中 TeamRun；在发起停止时调用，不等 Run 落定。

        ``agent_team`` 工具在线程里阻塞运行 Team，执行中途不检查取消信号：等 Run 落定再取消
        Team 会互等。Coordinator 检出 TeamRun 已取消就停下节点并返回，工具随之结束，Run 才能
        走到检查点。挂起的 Run 只可能带着待确认方案，同样在这里一并取消。失败只记录。
        """
        from app.core.tool.handler.agent_team.coordinator import get_agent_team_coordinator

        try:
            current = await asyncio.to_thread(self._tasks.require_current_run, self.task_id)
            if current is not None:
                await asyncio.to_thread(
                    get_agent_team_coordinator().cancel_for_parent_run_id, current.id
                )
        except Exception:
            log.exception(
                "task_team_cancel_failed",
                extra={"msg": "停止时取消 Agent Team 失败", "data": {"task_id": self.task_id}},
            )

    def _signal(self, reason: CancelReason) -> bool:
        """有执行者时打 Task 取消信号，返回是否打了；只在事件循环线程调用。

        判定与打信号之间没有 await，与 :meth:`_drive` 的「注销执行 + 取走信号」互斥：信号要么被这次
        执行的收尾取走，要么根本不打，不会残留到下一个执行。
        """
        if not self.driving:
            return False
        cancellation_registry.request_cancel(self.task_id, reason)
        return True

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
                if current is not None and self._phase(current) == "idle"
                else None
            )
            if resumed is None:
                raise SessionConflictError("RUN_NOT_RESUMABLE", "当前运行不可恢复")
            pending = await asyncio.to_thread(self._queue.pending_nowait, self.task_id)
            self._start(resumed, pending, fresh_run=False)
        self._publish_state()

    async def phase(self) -> dispatch_policy.Phase:
        """读当前 Run 得出执行阶段；供闸门外的调用方（HTTP 命令、Coordinator）判断活动。

        异常:
            KeyError: Task 不存在。
        """
        current = await asyncio.to_thread(self._tasks.require_current_run, self.task_id)
        return self._phase(current)

    async def wait_idle(self, wait_seconds: float) -> bool:
        """等待 Task 静止（``idle``），最多 ``wait_seconds`` 秒；返回是否已静止（挂起不算静止）。

        先取事件再判定：判定之后才发生的变化一定会置位这次取到的事件，不会漏醒。

        异常:
            KeyError: Task 不存在。
        """
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while True:
            changed = self.state_changed
            if await self.phase() == "idle":
                return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except TimeoutError:
                return False

    # ── 执行 ──────────────────────────────────────────────────────────────

    @property
    def driving(self) -> bool:
        """是否有执行者；同步判定，不读库——打取消信号必须与执行收尾之间没有 await。"""
        return self.execution is not None and not self.execution.done()

    def _phase(self, current: ConversationRunRecord | None) -> dispatch_policy.Phase:
        return dispatch_policy.phase_of(current, self.driving)

    def _start(
            self, run: ConversationRunRecord, pending: list[TaskMessage], *, fresh_run: bool
    ) -> None:
        """清空 mailbox、投递初始消息，再把 Run 交给后台执行；调用方持 Task 闸门。

        异常:
            ValueError: 该 Task 已有执行（调用方违约）。
        """
        if self.driving:
            raise ValueError(f"task {self.task_id} 已有执行")
        self.mailbox.reset()
        for message in dispatch_policy.initial_messages(pending, fresh_run=fresh_run):
            self.mailbox.post(message)
        self.execution = asyncio.get_running_loop().create_task(self._drive(run))

    async def _drive(self, run: ConversationRunRecord) -> None:
        """驱动一个 Run；失败由 ``RunDriver`` 落定。收尾按落定状态与取消原因传播、接续剩余消息。

        注销执行与取走 Task 取消信号之间没有 await（与 :meth:`_signal` 互斥），这是信号的唯一
        清理点。
        取消在最后一个检查点之后才到时 Run 可能已挂起或完成：挂起的直接迁移为 cancelled；收到取消时
        不接续剩余消息，停止之后才到的消息由 loop 在本执行结束后判定。
        """
        await RunDriver(self.mailbox).drive(run)
        self.execution = None
        reason = cancellation_registry.consume(self.task_id)
        if self._stopping:
            # 删除前停止：不接续、不传播，但执行已注销，仍要唤醒 ``wait_idle`` 的等待方重新判定，
            # 否则它们只能等到自己的超时。
            self._publish_state()
            return
        try:
            settled = await asyncio.to_thread(self._ledger.get_run, run.id)
            if reason is not None and settled.status == ConversationRunStatus.SUSPENDED.value:
                settled = await asyncio.to_thread(
                    self._ledger.transition, run.id, RunTransition.CANCEL
                ) or settled
            if settled.status == ConversationRunStatus.CANCELLED.value:
                await self._after_cancelled(settled, reason or "user")
            if settled.status in TERMINAL_RUN_STATUSES:
                await self._report_to_parent(settled, reason)
            if reason is None and settled.status in _CONTINUE_AFTER_STATUSES:
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
        self._publish_state()

    async def _after_cancelled(self, run: ConversationRunRecord, reason: CancelReason) -> None:
        """Run 落定 cancelled 后：向子 Task 传播，用户取消再给下一个 Run 留一条通知。

        Agent Team 不在这里：它已在发起停止时取消（见 :meth:`_cancel_teams`）。子 Task 一律以
        ``parent`` 原因停止，只发起、不等待：持自己的闸门去等子 Task 的闸门会让父子互等。通知以
        ``next_run`` 入队、不推进，随下一次推进写入；上游停止与编辑不留通知。传播失败只记录，不影响
        本 Run 终态。
        """
        from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

        try:
            children = await asyncio.to_thread(self._tasks.list_child_tasks, self.task_id)
            for child in children:
                task_runtime_spaces.cancel(child.id, "parent")
        except Exception:
            log.exception(
                "task_cancel_propagation_failed",
                extra={
                    "msg": "Run 取消后向子 Task 传播失败",
                    "data": {"task_id": self.task_id, "run_id": run.id},
                },
            )
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
        """委派子 Task 的 Run 到终态后，把结论以后台结果消息回投父 Task。

        父 Agent 据此被唤醒（挂起中）或开新 Run（已结束），不再轮询子 Task。两个落定点各调一次：
        执行收尾（:meth:`_drive`）与直接取消挂起的 Run（:meth:`cancel`）。父停止（``parent``）或
        编辑重跑（``edited``）导致的取消不回投：前者结论无人需要、回投会把父 Task 又推起来，后者
        紧接着就重跑。Team 节点 Task 不是 ``delegate_task``，结论由 Coordinator 收集。回投失败只
        记录，不影响本 Run 终态。
        """
        if reason in ("parent", "edited"):
            return
        from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

        try:
            task = await asyncio.to_thread(self._tasks.get_task, self.task_id)
            if task.task_type != TOOL_DELEGATE_TASK or task.parent_task_id is None:
                return
            body = BackgroundResultBody(
                tool_name=TOOL_DELEGATE_TASK,
                tool_result=json.dumps(
                    {
                        "child_task_id": self.task_id,
                        "status": run.status,
                        "end_reason": run.end_reason,
                        "final_output": run.final_output,
                    },
                    ensure_ascii=False,
                ),
            )
            await asyncio.to_thread(
                task_runtime_spaces.enqueue, task.parent_task_id, TaskMessage.of(body)
            )
        except Exception:
            log.exception(
                "child_result_report_failed",
                extra={
                    "msg": "子 Agent 结论回投父 Task 失败",
                    "data": {"task_id": self.task_id, "run_id": run.id},
                },
            )

    def _publish_state(self) -> None:
        """阶段可能已变化：换新事件并置位，唤醒 ``wait_idle`` 等待方重新判定。"""
        changed, self.state_changed = self.state_changed, asyncio.Event()
        changed.set()
