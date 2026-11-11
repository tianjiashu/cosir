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
  :meth:`_phase` 合成一个阶段（driving / suspended / idle），调度、取消、恢复、静止与 HTTP 侧的
  活动判断都只看阶段，不各自组合两者：执行者的释放滞后于 Run 落定。
- **执行登记归 ``TaskExecution``。** 至多一个后台执行，以及取消信号「有执行者才打、注销执行时取走」
  的不变量都在那里；actor 只决定何时启动、释放后怎么处理（:meth:`_after_drive`）。
- **取消编排归 actor。** 停止有两种语义（见 :meth:`cancel`）：用户停止只打断当前驱动，后台工作、
  Team 与子 Task 照常运行；上游停止另外收回这个 Task 交出去的一切（``TaskRecall``）。有执行者就交给
  ``TaskExecution.cancel`` 打信号，工作流在检查点落定；挂起的 Run 没有执行者，在闸门内直接迁移为
  cancelled。上游停止下游从工具或工作线程发起，经 :meth:`request_cancel` 投递到 actor 所在的事件
  循环、不等待。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast

from app.config.logging.logger import log
from app.core.context.context_store import ContextStore
from app.core.task_execution import TaskExecution
from app.core.task_recall import TaskRecall
from app.core.tool.background.background_registry import background_registry
from app.core.workflow.cancel.conversation_run_cancellation_registry import (
    CancelReason,
    cancel_end_reason,
)
from app.core.workflow.message_queue import dispatch_policy
from app.core.workflow.message_queue.task_message import TaskMessage
from app.models import ConversationRunRecord
from app.models.enums.conversation_run_status import (
    CONTINUE_AFTER_STATUSES,
    ConversationRunStatus,
    RunTransition,
)
from app.service import depends as service_depends

if TYPE_CHECKING:
    from concurrent.futures import Future

    from app.task_runtime.task_runtime_space import TaskRuntimeSpace


class SessionConflictError(ValueError):
    """当前 Task 状态不允许本次用户命令；``code`` 是稳定的机器码，API 映射为 409。

    ``retryable`` 表示「刷新状态后重试可能成功」，前端据此决定是否提供重新同步入口；冲突已成定局
    （如排队消息已被并入）时为 ``False``。
    """

    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable

class TaskActor:
    """一个 Task 的调度者；``start`` / ``stop`` 等方法只能在事件循环线程调用。"""

    def __init__(self, space: TaskRuntimeSpace) -> None:
        self.task_id = space.task_id
        self._space = space
        # Task 级投递通道（space 持有）：每次开始执行时清空，运行中 loop 往里投递，prepare_input 取光。
        self.mailbox = space.mailbox
        # 上游停止时收回交出去的工作；actor 只决定何时调用。
        self._recall = TaskRecall(self.task_id)
        # 执行登记：至多一个后台执行，释放后回到 :meth:`_after_drive`。
        self._execution = TaskExecution(self.task_id, self.mailbox, self._after_drive)
        self._loop_task: asyncio.Task[None] | None = None
        # actor 所在的事件循环，``start`` 时记下；:meth:`request_cancel` 据此从任意线程投递取消。
        self._event_loop: asyncio.AbstractEventLoop | None = None
        # 已判定过的最大消息 id：之后到达的才算新消息。
        # 外部队列「取出不删除」：行要等 prepare_input 写进 context 时才确认删除
        self._watermark = 0
        # 已推进完的最大消息 id：loop 判定并推进（含开出新 Run）之后才前移；它与 ``_watermark``
        # 之间的消息已被读到、还在推进途中，「无事可做」不能把它们当作已处理（见 :meth:`_quiescent`）。
        self._handled = 0
        self._stopping = False
        # loop 停下后置位：删除流程等它再删，进程关闭时也置位。
        self.stopped = asyncio.Event()
        # 阶段可能变化时换新并置位，``wait_quiescent`` 据此重新判定。
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

        不收回：删除入口已停下整棵所有权子树、排空 Team，后台工作在删除提交后撤销——删除没提交时
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
        self._publish_state()

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
                # 单轮失败只记录，不杀死 loop：队列行仍在，下一次被新消息唤醒时随之一并重读。
                # 水位已经前移（否则待处理行会让 ``pending`` 立刻返回、loop 空转），但「已推进完」
                # 不能跟着前移：那会让静止判定把这条消息当成已处理，等待方读走上一条 Run 的结论。
                log.exception(
                    "task_actor_advance_failed",
                    extra={"msg": "Task 调度推进失败", "data": {"task_id": self.task_id}},
                )
                self._publish_state()
                continue
            self._handled = self._watermark
            self._publish_state()

    async def _advance(self, pending: list[TaskMessage], fresh: list[TaskMessage]) -> None:
        """在 Task 闸门内按 ``dispatch_policy`` 推进一次：投递、唤醒、新开 Run 或不动。

        新建 Run 后先登记执行再发布快照：顺序反过来时，一次投影异常就会留下「running 但没有执行者」
        的 Task；快照发布属于观测旁路，失败只记录。
        """
        if self._stopping:
            return
        if fresh:
            # 有新消息入队：把排队投影同步到队列现状（入队方可能在自己的事务里写入，提交后才由这里读到）。
            await asyncio.to_thread(
                service_depends.get_conversation_task_state_service().refresh_queued_messages,
                self.task_id,
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
        self._publish_state()

    async def cancel(self, reason: CancelReason = "user") -> None:
        """停止这个 Task；只发起停止，不等执行落定。重复取消幂等。

        两种语义按 ``reason`` 区分：

        - ``user``（打断）：只停当前执行或挂起的 Run。后台工作、活动 Team（含待批准的方案）与子 Task 都
          不动——它们属于 Task，结论到达时照常唤醒或开新 Run；要停它们用看板逐项或全部停止。Task 空闲时
          什么也不做。
        - ``parent``（收回）：上游不再需要这个 Task 的任何结果，在打断之外收回它交出去的一切
          （``TaskRecall``）。有执行者时先打信号再收回：信号在时登记表拒收新登记、不交付结论（见
          ``background_registry``），收回之后这次驱动不会再留下后台工作；挂起的 Run 先收回再迁移：迁移之后
          才到的结论会被 loop 当作新消息开出新 Run。

        有执行者时打信号，由工作流在检查点落定、收尾时接续；挂起的 Run 没有执行者，在闸门内直接迁移为
        cancelled。快路径只打断、不发布状态：阶段仍是 ``driving``，等待方不必被唤醒。

        异常:
            KeyError: Task 不存在。
        """
        if self._execution.cancel(reason):
            if reason == "parent":
                await self._recall.recall()
            return
        async with self._space.async_operation():
            # 拿闸门期间可能已开出新执行：重新判定，不让信号落空或残留。
            if self._execution.cancel(reason):
                if reason == "parent":
                    await self._recall.recall()
            else:
                await self._cancel_suspended(reason)
        self._publish_state()

    def request_cancel(self, reason: CancelReason) -> None:
        """从任意线程发起 :meth:`cancel`，只投递、不等待；失败只记录。

        给上游停止下游用（收回向下传播、撤销委派）：调用方多在工具或工作线程里，没有可 await 的事件
        循环；在事件循环上的调用方（``TaskRecall``）正持自己的 Task 闸门，等对方的闸门会让父子互等。
        能 await 且可以等的调用方（HTTP 用户停止、Coordinator 停节点）直接用 :meth:`cancel`。

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

    async def _cancel_suspended(self, reason: CancelReason) -> None:
        """没有执行者：上游停止先收回，挂起的 Run 再直接迁移为 cancelled；调用方持 Task 闸门。

        收回在迁移之前（见 :meth:`cancel`）；Task 空闲时用户停止什么也不做，上游停止只收回。
        """
        if reason == "parent":
            await self._recall.recall()
        current = await asyncio.to_thread(self._tasks.require_current_run, self.task_id)
        if self._phase(current) == "suspended":
            await asyncio.to_thread(
                self._ledger.transition,
                current.id,
                RunTransition.CANCEL,
                end_reason=cancel_end_reason(reason),
            )

    async def resume(self) -> None:
        """当前 Run 为 cancelled / failed 且没有执行者时，从 prepare_input 续跑同一个 Run。

        不经 loop（恢复时通常没有新消息、loop 正挂起），但同样在 Task 闸门内判定与启动：用户恰好
        同时发了消息时，两边不会各开一个执行。删除前的停止（``_stopping``）之后拒绝：恢复是唯一
        不经 loop 就能开出执行的入口，放行会在即将删除的 Task 上留下执行者。这个 Run 的最后一个回合
        已被撤回时拒绝：没有可续的回合，新输出会接在已撤回的回合上。

        异常:
            SessionConflictError: ``RUN_NOT_RESUMABLE``。
            KeyError: Task 不存在。
        """
        async with self._space.async_operation():
            if self._stopping:
                raise SessionConflictError("RUN_NOT_RESUMABLE", "Task 正在删除，不可恢复")
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
                raise SessionConflictError("RUN_NOT_RESUMABLE", "当前运行不可恢复")
            pending = await asyncio.to_thread(self._queue.pending_nowait, self.task_id)
            # 恢复清空结束原因：把迁移前的记录交给驱动，「用户已停止」据此推导。
            self._start(resumed, pending, fresh_run=False, preceding_run=current)
        self._publish_state()

    async def phase(self) -> dispatch_policy.Phase:
        """读当前 Run 得出执行阶段；供闸门外的调用方（HTTP 命令、Coordinator）判断活动。

        异常:
            KeyError: Task 不存在。
        """
        current = await asyncio.to_thread(self._tasks.require_current_run, self.task_id)
        return self._phase(current)

    async def wait_quiescent(self, wait_seconds: float | None = None) -> bool:
        """等待 Task 无事可做，最多 ``wait_seconds`` 秒（``None`` 不限时）；返回是否已无事可做。

        给「投递一条输入、等它的结论」的调用方用（委派子 Agent、Team 节点）：落定后读 Task 最新
        Run 即是结论，不必记下是哪个 Run 处理了这条输入——并入运行中的 Run、与其他输入合并进同一
        个新 Run、Run 完成后接续剩余消息，最终结论都在最新 Run 上。只看阶段是否 ``idle`` 不够：
        投递之后、新 Run 开出来之前，以及 Run 完成后正要接续剩余消息时，Task 都是静止的。

        调度器已停止（正常停止或异常退出）时立即返回 ``False``：队列里留下的可推进消息不会再被
        处理，等下去不会有结果，由调用方按失败处理。

        异常:
            KeyError: Task 不存在（含等待期间被删除）。
        """
        return await self._wait_for(self._quiescent, wait_seconds)

    async def _quiescent(self) -> bool:
        """静止，且 actor 不会再主动推进：没有尚未推进完的可推进消息；最新 Run 完成（或尚无 Run）
        时队列里也没有可推进的消息——执行结束后会接续它们；取消、失败不自动接续，留着的消息等新
        消息到来再一起推进。系统通知不推进，不影响判定。

        后台工作属于 Task：Run 可以先结束，结论到达后再开新 Run 收尾，所以还有没交付完的后台工作就
        不算静止，否则等待方会读走「已在后台跑」的中间结论。登记表变化经 :meth:`notify_state_changed`
        唤醒等待方。

        调度器已停止时不报告静止：那些消息再也推不动，报静止会让等待方读走上一次 Run 的结论。"""
        if self.stopped.is_set():
            return False
        if background_registry.has_work(self.task_id):
            return False
        current = await asyncio.to_thread(self._tasks.require_current_run, self.task_id)
        if self._phase(current) != "idle":
            return False
        # 唤醒先写 running 再登记执行者，这里不持闸门，可能落在这个空档：还有人要跑，不算静止。
        if current is not None and current.status == ConversationRunStatus.RUNNING:
            return False
        continues = current is None or current.status == ConversationRunStatus.COMPLETED.value
        pending = await asyncio.to_thread(self._queue.pending_nowait, self.task_id)
        return not any(
            message.wakes(None) and (continues or cast(int, message.id) > self._handled)
            for message in pending
        )

    async def _wait_for(
            self, ready: Callable[[], Awaitable[bool]], wait_seconds: float | None
    ) -> bool:
        """反复判定 ``ready``，阶段变化时重判；先取事件再判定，判定之后的变化一定会置位这次取到
        的事件，不会漏醒。"""
        loop = asyncio.get_running_loop()
        deadline = None if wait_seconds is None else loop.time() + wait_seconds
        while True:
            changed = self.state_changed
            if await ready():
                return True
            if self.stopped.is_set():
                # 调度器不再推进，等待不会再有结果：交给调用方按失败处理，不无限挂起。
                return False
            if deadline is None:
                await changed.wait()
                continue
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except TimeoutError:
                return False

    # ── 执行 ──────────────────────────────────────────────────────────────

    @property
    def driving(self) -> bool:
        """是否有执行者；同步判定，不读库（见 ``TaskExecution.driving``）。"""
        return self._execution.driving

    def _phase(self, current: ConversationRunRecord | None) -> dispatch_policy.Phase:
        """由当前 Run 与执行登记得出执行阶段：驱动、取消、恢复、静止与闸门外判断共用这一处。

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
        """执行已释放（``TaskExecution`` 的回调）：按 Run 落定状态与取消原因接续剩余消息，最后唤醒
        等待方。

        ``reason`` 是收尾时取走的取消原因（没收到取消为 ``None``）。取消在最后一个检查点之后才到时
        Run 可能已挂起或完成：挂起的直接迁移为 cancelled；收到取消时不接续剩余消息，停止之后才到的
        消息由 loop 在本执行结束后判定。删除前停止（``_stopping``）不接续。接续失败只记录。
        """
        if self._stopping:
            # 执行已注销，仍要唤醒 ``wait_quiescent`` 的等待方重新判定，否则它们只能等到自己的超时。
            self._publish_state()
            return
        try:
            settled = await asyncio.to_thread(self._ledger.get_run, run.id)
            if reason is not None and settled.status == ConversationRunStatus.SUSPENDED.value:
                settled = await asyncio.to_thread(
                    self._ledger.transition,
                    run.id,
                    RunTransition.CANCEL,
                    end_reason=cancel_end_reason(reason),
                ) or settled
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
        self._publish_state()

    def notify_state_changed(self) -> None:
        """从任意线程告知「静止判定的输入变了」（后台结果登记表变化），在 actor 的事件循环上唤醒等待方。

        actor 尚未拉起或事件循环已关闭时没有等待方可唤醒，直接返回。
        """
        loop = self._event_loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._publish_state)

    def _publish_state(self) -> None:
        """阶段可能已变化：换新事件并置位，唤醒 ``wait_quiescent`` 等待方重新判定。"""
        changed, self.state_changed = self.state_changed, asyncio.Event()
        changed.set()
