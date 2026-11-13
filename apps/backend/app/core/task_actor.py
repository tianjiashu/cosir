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
  一次：完成、挂起时接续剩余消息（新 Run 只取一条用户输入，排队消息因此一轮一条依次处理）；
  取消、失败不自动接续，持续失败也不会空转。
- **停止期间先等执行落定再判定。** 已发起停止时 loop 醒来不往即将作废的 mailbox 投递，而是等这次
  执行（含收尾）结束，再按 ``idle`` 照常判定：用户停下后又发的消息就是这一轮的新消息，自然开新
  Run；停止前排着的不是新消息，不自己跑起来。
- **「判定 + 启动执行」在 Task 闸门内串行。** loop、执行结束后的接续与 HTTP 的恢复命令都在闸门内
  读 Run 状态再启动，同一 Task 不会出现两个执行者；闸门同时与删除、fork 互斥。执行期间不持闸门。
- **执行阶段单一判定。** Run 状态（持久化业务事实）与执行登记（进程内是否有执行者）只在
  :meth:`_phase` 合成一个阶段（driving / suspended / idle），调度、取消、恢复与 HTTP 侧的
  活动判断都只看阶段，不各自组合两者：执行者的释放滞后于 Run 落定。
- **执行登记归 ``TaskExecution``。** 至多一个后台执行，以及取消信号「有执行者才打、注销执行时取走」
  的不变量都在那里；actor 只决定何时启动、释放后怎么处理（:meth:`_after_drive`）。
- **取消编排归 actor。** 停止有两种语义（见 :meth:`cancel`）：用户停止只打断当前驱动，后台工作与
  子 Task 照常运行；上游停止另外收回这个 Task 交出去的一切（``TaskRecall``）。有执行者就交给
  ``TaskExecution.cancel`` 打信号，工作流在检查点落定；挂起的 Run 没有执行者，在闸门内直接迁移为
  cancelled。上游停止下游从工具或工作线程发起，经 :meth:`request_cancel` 投递到 actor 所在的事件
  循环、不等待。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, cast

from app.config.logging.logger import log
from app.core.context.context_store import ContextStore
from app.core.events import TaskQueueChangedEvent
from app.core.events.event_sink import event_sink
from app.core.task_execution import TaskExecution
from app.core.task_recall import TaskRecall
from app.core.tool.multi_agent import derived_report
from app.core.workflow.cancel.conversation_run_cancellation_registry import (
    CancelReason,
    cancel_end_reason,
)
from app.core.workflow.message_queue import dispatch_policy
from app.core.workflow.message_queue.task_message import TaskMessage
from app.models import ConversationRunRecord
from app.models.enums.conversation_run_status import (
    CONTINUE_AFTER_STATUSES,
    TERMINAL_RUN_STATUSES,
    ConversationRunStatus,
    RunTransition,
)
from app.models.errors.task_command_errors import TaskCommandConflictError
from app.service import depends as service_depends

if TYPE_CHECKING:
    from concurrent.futures import Future

    from app.task_runtime.task_runtime_space import TaskRuntimeSpace


class TaskActor:
    """一个 Task 的调度者；``start`` / ``stop`` 等方法只能在事件循环线程调用。"""

    def __init__(self, space: TaskRuntimeSpace) -> None:
        self.task_id = space.task_id
        self._space = space
        # Task 级投递通道（space 持有）：每次开始执行时清空，运行中 loop 往里投递，prepare_input 取光。
        self.mailbox = space.mailbox
        # 上游停止时收回交出去的工作；actor 只决定何时调用。
        self._recall = TaskRecall(self.task_id)
        # 有执行者时收回的后台调用：驱动释放时再结算一次（见 :meth:`_recall_work`）。
        self._recalled: dict[str, dict[str, object] | None] = {}
        # 执行登记：至多一个后台执行，释放后回到 :meth:`_after_drive`。
        self._execution = TaskExecution(self.task_id, self.mailbox, self._after_drive)
        self._loop_task: asyncio.Task[None] | None = None
        # actor 所在的事件循环，``start`` 时记下；:meth:`request_cancel` 据此从任意线程投递取消。
        self._event_loop: asyncio.AbstractEventLoop | None = None
        # 已判定过的最大消息 id：之后到达的才算新消息。
        # 外部队列「取出不删除」：行要等 prepare_input 写进 context 时才确认删除
        self._watermark = 0
        self._stopping = False
        # loop 停下后置位：删除流程等它再删，进程关闭时也置位。
        self.stopped = asyncio.Event()

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
        """在当前事件循环上拉起监听 loop；重复调用无效果。

        loop 停下（正常停止或异常退出）后不自动重建：``stopped`` 置位后由调用方决定怎么办，
        不在故障根因仍在时反复拉起同一个 loop。
        """
        if self._loop_task is None:
            self._event_loop = asyncio.get_running_loop()
            self._loop_task = self._event_loop.create_task(self._loop())
            # 兜底：漏出 ``_loop`` 的异常在这里留下日志，不留无人取回失败的 task。
            self._loop_task.add_done_callback(self._on_loop_done)

    async def stop(self) -> None:
        """Task 即将删除：停止当前执行并等它落定，再停下 loop。不调度后续 Run。

        不收回：删除入口已停下整棵所有权子树，后台工作在删除提交后撤销——删除没提交时
        Task 仍在，交出去的工作不该丢。
        """
        self._stopping = True
        if self._execution.cancel("parent"):
            await self._execution.wait_released()
        await self._cancel_loop()

    async def abort(self) -> None:
        """进程关闭：直接取消执行与 loop，不等工作流落定；遗留 running Run 由下次启动收敛。"""
        self._stopping = True
        await self._execution.abort()
        await self._cancel_loop()

    async def _cancel_loop(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        self._halt_loop()

    def _halt_loop(self) -> None:
        """loop 不再推进：置位 ``stopped`` 并唤醒等待方。

        正常停止与异常退出共用这一个终态——对外没有区别，区别只体现在日志事件上。停止不可逆，
        不自动重建 loop。
        """
        self.stopped.set()

    def _on_loop_done(self, done: asyncio.Task[None]) -> None:
        """loop 结束的兜底：异常退出在这里落一条日志并进入停止态，不留静默死掉的调度器。"""
        if done.cancelled():
            return
        error = done.exception()
        if error is None:
            return
        log.error(
            "task_actor_loop_crashed",
            extra={
                "msg": "Task 调度器异常退出，已停止推进",
                "data": {"task_id": self.task_id},
            },
            exc_info=error,
        )
        self._halt_loop()

    # ── 监听与推进 ────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        """常驻监听：按水位等新消息，醒来后推进一次；单轮推进失败只记录，不杀死 loop。

        ``pending`` 是该 Task 全部未确认的消息（含已判定过、尚未被 ``prepare_input`` 消费的），回答
        「现在队列里有什么」；``fresh`` 是其中 id 大于水位的那部分，回答「这次是什么把我叫醒的」——
        只有它们触发推进，残留消息随下一次推进一并投递。已发起停止时先等本次执行（含收尾）落定
        再判定，避免往即将作废的 mailbox 投递。

        读队列本身失败（库不可用、行反序列化失败、停止中等待执行失败）时不重试：记录后进入
        :meth:`_halt_loop` 的停止态，让「调度器不可用」成为一个明确、可观察的事实，而不是把失败
        伪装成还在等待。
        """
        while True:
            try:
                pending = await self._queue.pending(self.task_id, self._watermark)
                fresh = [message for message in pending if cast(int, message.id) > self._watermark]
                self._watermark = max(cast(int, message.id) for message in pending)
                if self._execution.stopping:
                    # 停止中：等执行与收尾结束再判定；loop 被取消时不连带取消执行。
                    await self._execution.wait_released()
            except Exception:
                log.exception(
                    "task_actor_loop_failed",
                    extra={
                        "msg": "Task 调度器读队列失败，已停止推进",
                        "data": {"task_id": self.task_id},
                    },
                )
                self._halt_loop()
                return
            try:
                await self._advance(pending, fresh)
            except Exception:
                # 单轮失败只记录，不杀死 loop：队列行仍在，下一次被新消息唤醒时随之一并重读。水位已经
                # 前移，否则待处理行会让 ``pending`` 立刻返回、loop 空转。
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
            # 有新消息入队：交出「队列变了」的事实，由消费方把排队投影同步到队列现状（入队方可能在
            # 自己的事务里写入，提交后才由这里读到）。
            await asyncio.to_thread(event_sink(), TaskQueueChangedEvent(task_id=self.task_id))
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
                    self._start(woken, pending, fresh_run=False, preceding_run=None)
            elif isinstance(action, dispatch_policy.StartRun):
                launch = action.launch
                # 模型在配置文件里，Run 只存 id：模型被删或文件写坏后，发起配置可能
                # 指向不存在的模型。开不了新 Run 就不动，消息留在队列里等用户换模型再发，
                # 与原来外键置空后 ``RunLaunch.from_run`` 返回 None 的「不动」一致；
                # ``dispatch_policy`` 保持纯函数，不读文件。
                models = service_depends.get_model_config_service()
                if not await asyncio.to_thread(models.exists, launch.model_config_id):
                    log.warning(
                        "task_actor_model_unavailable",
                        extra={
                            "msg": "发起配置的模型不可用，暂不开新 Run",
                            "data": {"task_id": self.task_id, "model": launch.model_config_id},
                        },
                    )
                else:
                    # 新 Run 只改当前活动：``create_run`` 提交后投影 RunInitializedEvent，前端经
                    # 增量收到，不重建、不推整份快照（长对话里每轮重发全部历史）。
                    run = await asyncio.to_thread(self._ledger.create_run, self.task_id, launch)
                    self._start(run, pending, fresh_run=True, preceding_run=current)

    async def cancel(self, reason: CancelReason = "user") -> None:
        """停止这个 Task；只发起停止，不等执行落定。重复取消幂等。

        两种语义按 ``reason`` 区分：

        - ``user``（打断）：只停当前执行或挂起的 Run。后台工作与子 Task 都不动——它们属于 Task，
          结论到达时照常唤醒或开新 Run；要停它们各按自己的入口（后台命令在看板上逐项或全部停止，子 Task
          对它本身停止）。Task 空闲时什么也不做。
        - ``parent``（收回）：上游不再需要这个 Task 的任何结果，在打断之外收回它交出去的一切
          （``TaskRecall``）。有执行者时先打信号再收回：信号在时登记表拒收新登记、不交付结论（见
          ``command_results``），收回之后这次驱动不会再留下后台工作；挂起的 Run 先收回再迁移：迁移之后
          才到的结论会被 loop 当作新消息开出新 Run。

        有执行者时打信号，由工作流在检查点落定、收尾时接续；挂起的 Run 没有执行者，在闸门内直接迁移为
        cancelled。快路径只打断、不发布状态：阶段仍是 ``driving``，等待方不必被唤醒。

        异常:
            KeyError: Task 不存在。
        """
        if self._execution.cancel(reason):
            if reason == "parent":
                await self._recall_work(driving=True)
            return
        async with self._space.async_operation():
            # 拿闸门期间可能已开出新执行：重新判定，不让信号落空或残留。
            if self._execution.cancel(reason):
                if reason == "parent":
                    await self._recall_work(driving=True)
            else:
                await self._cancel_suspended(reason)

    def request_cancel(self, reason: CancelReason) -> None:
        """从任意线程发起 :meth:`cancel`，只投递、不等待；失败只记录。

        给上游停止下游用（收回向下传播、撤销委派）：调用方多在工具或工作线程里，没有可 await 的事件
        循环；在事件循环上的调用方（``TaskRecall``）正持自己的 Task 闸门，等对方的闸门会让父子互等。
        能 await 且可以等的调用方（HTTP 用户停止）直接用 :meth:`cancel`。

        actor 尚未拉起（注册表还没 ``bind_loop``）或事件循环已关闭（进程退出中）时没有执行取消的
        地方，记录后放弃：前者只在启动瞬间出现，后者遗留的 running Run 由下次启动收敛。
        """
        loop = self._event_loop
        if loop is None or loop.is_closed():
            log.warning(
                "task_cancel_request_dropped",
                extra={
                    "msg": "actor 未在运行的事件循环上，放弃发起停止",
                    "data": {"task_id": self.task_id, "reason": reason, "started": loop is not None},
                },
            )
            return
        future = asyncio.run_coroutine_threadsafe(self.cancel(reason), loop)
        future.add_done_callback(self._log_cancel_failure)

    def _log_cancel_failure(self, done: Future[None]) -> None:
        if not done.cancelled() and done.exception() is not None:
            log.error(
                "task_cancel_failed",
                extra={"msg": "发起停止 Task 失败", "data": {"task_id": self.task_id}},
                exc_info=done.exception(),
            )

    async def _recall_work(self, *, driving: bool) -> None:
        """收回交出去的工作并结算被撤销的后台卡片。

        有执行者时同一批工具的占位行要等 observe 写入，此刻结算会扑空：先把被撤销的调用记下、交给
        :meth:`_after_drive` 在驱动释放后再结算一次，再立即结算已写入的行。记下必须在立即结算之前——
        结算要 await，驱动可能在这期间释放；早于记下就释放的驱动，它的行在立即结算时都已写入。
        """
        revoked = await self._recall.recall()
        if driving:
            self._recalled.update(revoked)
        await self._recall.settle(revoked)

    async def _cancel_suspended(self, reason: CancelReason) -> None:
        """没有执行者：上游停止先收回，挂起的 Run 再直接迁移为 cancelled；调用方持 Task 闸门。

        收回在迁移之前（见 :meth:`cancel`）；Task 空闲时用户停止什么也不做，上游停止只收回。
        """
        if reason == "parent":
            await self._recall_work(driving=False)
        current = await asyncio.to_thread(self._tasks.require_current_run, self.task_id)
        if self._phase(current) == "suspended":
            cancelled = await asyncio.to_thread(
                self._ledger.transition,
                current.id,
                RunTransition.CANCEL,
                end_reason=cancel_end_reason(reason),
            )
            if cancelled is not None:
                await self._report_settled(cancelled)

    async def resume(self) -> None:
        """当前 Run 为 cancelled / failed 且没有执行者时，从 prepare_input 续跑同一个 Run。

        不经 loop（恢复时通常没有新消息、loop 正挂起），但同样在 Task 闸门内判定与启动：用户恰好
        同时发了消息时，两边不会各开一个执行。删除前的停止（``_stopping``）之后拒绝：恢复是唯一
        不经 loop 就能开出执行的入口，放行会在即将删除的 Task 上留下执行者。这个 Run 的最后一个回合
        已被撤回时拒绝：没有可续的回合，新输出会接在已撤回的回合上。

        异常:
            TaskCommandConflictError: ``RUN_NOT_RESUMABLE``。
            KeyError: Task 不存在。
        """
        async with self._space.async_operation():
            if self._stopping:
                raise TaskCommandConflictError("RUN_NOT_RESUMABLE", "Task 正在删除，不可恢复")
            current = await asyncio.to_thread(self._tasks.require_current_run, self.task_id)
            resumed = None
            if (
                current is not None
                and self._phase(current) == "idle"
                and not await asyncio.to_thread(
                    ContextStore().run_last_turn_withdrawn, self.task_id, current.id
                )
            ):
                resumed = await asyncio.to_thread(
                    self._ledger.transition, current.id, RunTransition.RESUME
                )
            if resumed is None:
                raise TaskCommandConflictError("RUN_NOT_RESUMABLE", "当前运行不可恢复")
            pending = await asyncio.to_thread(self._queue.pending_nowait, self.task_id)
            # 恢复清空结束原因：把迁移前的记录交给驱动，「用户已停止」据此推导。
            self._start(resumed, pending, fresh_run=False, preceding_run=current)

    async def phase(self) -> dispatch_policy.Phase:
        """读当前 Run 得出执行阶段；供闸门外的调用方（HTTP 命令）判断活动。

        异常:
            KeyError: Task 不存在。
        """
        current = await asyncio.to_thread(self._tasks.require_current_run, self.task_id)
        return self._phase(current)

    # ── 执行 ──────────────────────────────────────────────────────────────

    @property
    def driving(self) -> bool:
        """是否有执行者；同步判定，不读库（见 ``TaskExecution.driving``）。"""
        return self._execution.driving

    def _phase(self, current: ConversationRunRecord | None) -> dispatch_policy.Phase:
        """由当前 Run 与执行登记得出执行阶段：驱动、取消、恢复与闸门外判断共用这一处。

        有执行者即 ``driving``，不看 Run 状态——workflow 先写终态或挂起，执行者还要跑完 STOP Hook、
        收束工具调用才释放，这段时间另开执行会出现两个执行者。不存在「running 但没有执行者」：新 Run
        与执行登记在同一次 Task 闸门内完成，``RunDriver`` 返回前一定把 Run 落定为终态或挂起，重启时
        遗留的 running Run 先由 lifespan 收敛为 cancelled。
        """
        if self.driving:
            return "driving"
        if current is not None and current.status == ConversationRunStatus.SUSPENDED:
            return "suspended"
        return "idle"

    def _start(
            self,
            run: ConversationRunRecord,
            pending: list[TaskMessage],
            *,
            fresh_run: bool,
            preceding_run: ConversationRunRecord | None,
    ) -> None:
        """按 ``dispatch_policy`` 选出初始消息，把 Run 交给执行登记；调用方持 Task 闸门。

        ``preceding_run`` 是这次驱动之前 Task 的当前 Run（见 ``RuntimeContext.preceding_run``）。

        异常:
            ValueError: 该 Task 已有执行（调用方违约）。
        """
        self._execution.start(
            run, dispatch_policy.initial_messages(pending, fresh_run=fresh_run), preceding_run
        )


    async def _after_drive(self, run: ConversationRunRecord, reason: CancelReason | None) -> None:
        """执行已释放（``TaskExecution`` 的回调）：Run 落定为终态时汇报给派生方，再按落定状态与取消原因
        接续剩余消息，最后唤醒等待方。

        ``reason`` 是收尾时取走的取消原因（没收到取消为 ``None``）。取消在最后一个检查点之后才到时
        Run 可能已挂起或完成：挂起的直接迁移为 cancelled；收到取消时不接续剩余消息，停止之后才到的
        消息由 loop 在本执行结束后判定。删除前停止（``_stopping``）不接续。接续失败只记录。
        """
        recalled, self._recalled = self._recalled, {}
        if self._stopping:
            return
        # 收回时这次驱动还没写完的后台占位，现在都已写入（见 :meth:`_recall_work`）。
        await self._recall.settle(recalled)
        try:
            settled = await asyncio.to_thread(self._ledger.get_run, run.id)
            if reason is not None and settled.status == ConversationRunStatus.SUSPENDED.value:
                settled = await asyncio.to_thread(
                    self._ledger.transition,
                    run.id,
                    RunTransition.CANCEL,
                    end_reason=cancel_end_reason(reason),
                ) or settled
            await self._report_settled(settled)
            if reason is None and settled.status in CONTINUE_AFTER_STATUSES:
                pending = await asyncio.to_thread(self._queue.pending_nowait, self.task_id)
                if pending:
                    # 剩余消息都没被这个 Run 消费：当作新到达的消息再判定一次。
                    await self._advance(pending, pending)
                    return
        except Exception:
            log.exception(
                "task_actor_settle_failed",
                extra={
                    "msg": "Run 落定后接续剩余消息失败",
                    "data": {"task_id": self.task_id, "run_id": run.id},
                },
            )

    async def _report_settled(self, run: ConversationRunRecord) -> None:
        """Run 的唯一汇报点：落定为终态（挂起不算）时交给派生汇报，规则都在 ``derived_report``。

        两个落定点（执行释放、挂起 Run 被取消）都经这里；汇报失败只记录，不影响本 Task 的结局与接续。
        """
        if run.status not in TERMINAL_RUN_STATUSES:
            return
        try:
            await asyncio.to_thread(derived_report.report_settled, run)
        except Exception:
            log.exception(
                "derived_report_failed",
                extra={
                    "msg": "Run 落定后向派生方汇报失败",
                    "data": {"task_id": self.task_id, "run_id": run.id},
                },
            )
