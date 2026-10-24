"""Task 级调度入口（inbox）：接收用户输入、编辑排队消息、编辑重跑、系统消息、取消后恢复、取消、
工具取消与删除前停止。设计见 ``docs/plan/task-mailbox-suspend-wake-design.md`` 与
``docs/plan/task-actor-dispatch-design.md``。

本类只负责投递：解析输入（文件 IO 在这里完成）、把请求变成信号发给该 Task 的
:class:`~app.service.conversation_run.task_actor.TaskActor`，再等 actor 兑现回执。是否新建、唤醒
Run、投递给当前执行、何时交接，全部由 actor 在其串行循环里按 ``dispatch_policy`` 判定；这是新建、
唤醒 Run 与启动执行的唯一路径（用户对话、子 Agent、Team 节点、后台结果都经过这里），因此同一 Task
不会出现两个执行者。

所有入口都以 Task 为目标（两种编辑以消息为目标），不接受 run_id：Run 身份由后端按 Task 的最新 Run
解析。actor 按需拉起：首次收到信号时创建，没有信号时挂起在信号队列上；Task 删除前经
:meth:`stop_tasks` 停下后退出，进程关闭时统一取消。

本模块不感知 Assistant Transport 的 wire 格式；回执里的 snapshot 由接入层读取。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast

from app.config.logging.logger import log
from app.core.workflow.cancel.conversation_run_cancellation_registry import (
    CancelReason,
    cancellation_registry,
)
from app.core.workflow.cancel.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.models import ConversationRunCommand
from app.models.conversation_run_options import ReasoningEffort
from app.models.enums.conversation_run_status import TERMINAL_RUN_STATUSES
from app.models.task_message import (
    Delivery,
    MessageBody,
    NoticeBody,
    TaskMessage,
    UserInputBody,
)
from app.service import depends as service_depends
from app.service.conversation_run import message_edit
from app.service.conversation_run.task_actor import Execution, TaskActor
from app.service.conversation_run.task_signals import (
    Cancel,
    DeliverPending,
    Enqueue,
    Rerun,
    Resume,
    SessionConflictError,
    SessionReceipt,
    Signal,
    Stop,
)

__all__ = ["SessionConflictError", "SessionReceipt", "WorkflowSession"]


def _log_actor_exit(task: asyncio.Task[None]) -> None:
    """actor 循环只应在 Stop 或进程关闭时结束；异常结束说明信号处理之外出了错，必须留痕。"""
    if not task.cancelled() and task.exception() is not None:
        log.error(
            "task_actor_loop_crashed",
            extra={"msg": "Task actor 循环异常退出"},
            exc_info=task.exception(),
        )


class WorkflowSession:


    def __init__(self) -> None:
        # task_id → actor；事件循环内状态，只在事件循环线程读写。
        self._actors: dict[int, TaskActor] = {}
        self._closing = False
        # actor 所在的事件循环；首次拉起 actor 时记录，供其他线程的配置广播跨线程投递。
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks = service_depends.get_task_service()
        self._queue = service_depends.get_task_message_queue()
        self._inputs = service_depends.get_user_input_service()

    async def submit(
            self,
            task_id: int,
            command: ConversationRunCommand,
            *,
            model_config_id: int,
            reasoning_effort: ReasoningEffort | None,
            delivery: Delivery = "immediate",
            agent_id: str = "main_agent",
    ) -> SessionReceipt:
        """接收一条用户输入：解析后交给 actor 入外部队列并推进。

        解析（附件校验等文件 IO）在 actor 外完成。静止时推进交接新 Run；有执行时按 ``delivery``
        投递；挂起时 ``immediate`` 输入唤醒同一个 Run。``agent_id`` 随消息入队，交接时成为新 Run 的
        Agent 身份（Team 节点的冻结定义由 ``run_driver`` 从节点 Task 快照推导）。工具线程经
        ``run_coroutine_threadsafe`` 在主事件循环上调用。

        异常:
            ValueError: 输入不合法（附件不可用、模型不支持图片等）。
            KeyError: Task 不存在。
        """
        body: UserInputBody = await asyncio.to_thread(
            self._inputs.prepare, task_id, command, model_config_id, reasoning_effort, agent_id
        )
        message = TaskMessage.of(body, delivery=delivery)
        return await self._receipt(task_id, Enqueue, message, by_user=True, admit=False)

    async def post_async(self, task_id: int, body: MessageBody) -> SessionReceipt:
        """在事件循环上投递一条系统消息（用户决定、后台结果），随后推进。

        actor 入队前按 ``dispatch_policy.admit`` 过滤：用户取消后被停止的下游结果直接丢弃并记录。
        推进不是用户发起的：可以唤醒挂起的 Run、让已完成的 Task 继续，但不会从取消、失败后开 Run。

        异常:
            KeyError: Task 不存在。
        """
        message = TaskMessage.of(body)
        return await self._receipt(task_id, Enqueue, message, by_user=False, admit=True)

    def post_nowait(self, task_id: int, body: MessageBody) -> None:
        """actor 之间回投系统消息（子 Agent 结论）：只发信号、不等回执，父子 actor 不互相等待。"""
        self._actor(task_id).send(Enqueue(TaskMessage.of(body), by_user=False, admit=True))

    def post(self, task_ids: list[int], body: NoticeBody) -> None:
        """持久化一条配置广播通知到这些 Task 的外部队列，并投递给其中正在执行的 Task；任意线程可调。

        通知不唤醒、不启动 Run：空闲或挂起的 Task 只留在队列里，下次执行开始时随初始投递并入。投递
        经事件循环异步调度、不等待结果——消息已落库，投递丢失只会推迟到下次执行。
        """
        self._queue.put_to_tasks(task_ids, TaskMessage.of(body))
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._deliver_pending, list(task_ids))

    def _deliver_pending(self, task_ids: list[int]) -> None:
        # 只通知已有 actor：没有 actor 就没有执行，消息留给下次执行的初始投递。
        for task_id in task_ids:
            actor = self._actors.get(task_id)
            if actor is not None:
                actor.send(DeliverPending())

    async def edit_queued(
            self, task_id: int, message_id: int, command: ConversationRunCommand
    ) -> SessionReceipt:
        """原地编辑一条排队中的用户输入：身份、``delivery`` 与发起配置都不变，只改正文与附件。

        不经 actor：这是对队列行的条件更新，与 ``prepare_input`` 的消费确认由「行仍存在才更新」
        仲裁，与 Run 状态无关。按原配置的模型校验能力（图片、推理强度），因为消息最终在那个模型上
        运行。已投递进 mailbox 的旧副本无需撤回：``prepare_input`` 并入时以确认删除返回的行内容
        为准。消息已被并入时不会转成重跑：要改已并入的消息，调用方显式走 :meth:`rerun`。

        异常:
            SessionConflictError: ``MESSAGE_CONSUMED``（消息已不在队列中）。
            ValueError: 输入不合法。
            KeyError: Task 不存在。
        """
        queued = await asyncio.to_thread(self._queued_user_input, task_id, message_id)
        if queued is None:
            raise SessionConflictError("MESSAGE_CONSUMED", "该消息已被处理，无法再编辑")
        previous = cast(UserInputBody, queued.body)
        launch = previous.launch
        prepared = await asyncio.to_thread(
            self._inputs.prepare,
            task_id,
            command,
            launch.model_config_id,
            launch.options.reasoning_effort,
            launch.agent_id,
            reuse=previous.message.presentation(),
        )
        body = prepared.model_copy(update={"launch": launch})
        updated = TaskMessage.of(body, delivery=queued.delivery, message_id=message_id)
        if not await asyncio.to_thread(self._queue.update, task_id, message_id, updated):
            raise SessionConflictError("MESSAGE_CONSUMED", "该消息已被处理，无法再编辑")
        await asyncio.to_thread(
            service_depends.get_conversation_task_state_service().refresh_queued_inputs, task_id
        )
        return SessionReceipt(queued=True)

    async def rerun(
            self,
            task_id: int,
            message_id: int,
            command: ConversationRunCommand,
            *,
            model_config_id: int,
            reasoning_effort: ReasoningEffort | None,
    ) -> SessionReceipt:
        """编辑最新一条已并入的用户输入并重跑：解析后交给 actor 两阶段处理（停止当前 Run → 移出
        它及其后 context + 以新消息入队 → 按本次请求的配置开新 Run）。

        异常:
            SessionConflictError: ``RUN_NOT_STOPPED``（停止落空，或上一次重跑仍在停止当前 Run）。
            ValueError: 目标不是最新一条已投影的用户输入；或输入不合法。
            KeyError: Task 不存在。
        """
        target = await asyncio.to_thread(message_edit.latest_user_input, task_id)
        if target is None or target[0] != message_id:
            raise ValueError("只能编辑最新用户消息")
        # 先取最新 Run：它同时校验 Task 存在，并提供重跑时的 Agent 身份（Task 不绑定 Agent，
        # 身份随发起配置走；子 Agent / Team 节点的 Task 不能因为一次编辑退回 main_agent）。
        latest = await asyncio.to_thread(self._tasks.require_latest_run, task_id)
        body = await asyncio.to_thread(
            self._inputs.prepare,
            task_id,
            command,
            model_config_id,
            reasoning_effort,
            latest.agent_id if latest is not None and latest.agent_id else "main_agent",
            reuse=target[1],
        )
        return await self._receipt(task_id, Rerun, message_id, body)

    def _queued_user_input(self, task_id: int, message_id: int) -> TaskMessage | None:
        return next(
            (
                message
                for message in self._queue.pending(task_id)
                if message.id == message_id and isinstance(message.body, UserInputBody)
            ),
            None,
        )

    async def resume(self, task_id: int) -> SessionReceipt:
        """最新 Run 为 cancelled / failed 时从 prepare_input 续跑同一个 Run。

        异常:
            SessionConflictError: ``RUN_NOT_RESUMABLE``。
            KeyError: Task 不存在。
        """
        return await self._receipt(task_id, Resume)

    async def cancel(self, task_id: int, reason: CancelReason = "user") -> bool:
        """停止 Task 当前的执行或挂起的 Run；没有可停止的对象时返回 ``False``。

        运行中的执行在这里同步打信号、立即返回，不排在其他信号之后；其余情况交给 actor。

        异常:
            KeyError: Task 不存在。
        """
        actor = self._actors.get(task_id)
        if actor is not None and actor.signal_execution(reason):
            return True
        return cast(bool, await self._request(task_id, Cancel, reason))

    def cancel_nowait(self, task_id: int, reason: CancelReason) -> None:
        """actor 之间传播取消：运行中同步打信号，否则发信号，不等回执。"""
        actor = self._actor(task_id)
        if not actor.signal_execution(reason):
            actor.send(Cancel(reason))

    async def stop_tasks(self, task_ids: set[int] | list[int]) -> None:
        """删除前停止这些 Task 的执行与挂起的 Run，等各自 actor 停下退出；须在删除闸门外调用。

        按上游停止处理：被删除的 Task 不需要「用户已取消」通知。
        """
        await asyncio.gather(
            *(self._request(task_id, Stop) for task_id in task_ids)
        )

    async def wait_run_settled(self, task_id: int, run_id: int) -> None:
        """等驱动 ``run_id`` 的执行完成收尾（含为剩余消息交接新 Run）；该 Run 已没有执行时立即返回。

        供跨 Run 订阅方在 Run 正常完成后找到交接出的新 Run；只等这一个 Run，不等之后的执行。
        """
        execution = self._execution(task_id)
        if execution is None or execution.run.id != run_id:
            return
        await execution.settled.wait()

    async def wait_idle(self, task_id: int, wait_seconds: float) -> bool:
        """等待 Task 静止，最多 ``wait_seconds`` 秒；返回是否已静止。

        静止 = 没有执行者且最新 Run 已到终态（或尚无 Run）；挂起不算静止。子 Agent 与 Team 节点只按
        Task 等结论，不跟踪某一条 Run：交接出的后续 Run 也在等待范围内。静止由 actor 在每批信号处理
        完后重算，交接在同一批内完成，不会把交接间隙误判为静止；取消、失败后残留的用户输入不会自行
        推进（等下一次发送），所以不要求外部队列为空。超时只表示还没静止，调用方自行检查取消后再等。

        异常:
            KeyError: Task 不存在。
        """
        latest = await asyncio.to_thread(self._tasks.require_latest_run, task_id)
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while True:
            actor = self._actors.get(task_id)
            if actor is None:
                # 没有 actor 就没有执行者；读到的最新 Run 即事实。
                return latest is None or latest.status in TERMINAL_RUN_STATUSES
            if actor.quiescent:
                return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(actor.state_changed.wait(), remaining)
            except TimeoutError:
                return False
            latest = await asyncio.to_thread(self._tasks.require_latest_run, task_id)

    def cancel_tool_call(self, task_id: int, tool_call_id: str) -> int | None:
        """标记 Task 当前执行中一次工具调用的取消，Run 继续；返回其 run_id。

        不经 actor：这是对执行的旁路控制。Task 没有执行或该调用已标记过时返回 ``None``。工具级信号
        仍按 ``(run_id, tool_call_id)`` 记录，随单次调用结束释放，Run 结束时兜底清理。
        """
        execution = self._execution(task_id)
        if execution is None:
            return None
        run_id = execution.run.id
        if tool_call_cancellation_registry.is_cancelled(run_id, tool_call_id):
            return None
        tool_call_cancellation_registry.mark_cancelled(run_id, tool_call_id)
        return run_id

    async def close(self) -> None:
        """应用关闭：先发 Task 信号（线程/子进程工具只认信号），再取消全部执行与 actor 并等待。

        不调度后续 Run；遗留的 running Run 由下次启动的 ``recover_orphaned_runs`` 收敛。
        """
        self._closing = True
        actors = list(self._actors.values())
        self._actors.clear()
        tasks: list[asyncio.Task[None]] = []
        for actor in actors:
            if actor.execution is not None:
                cancellation_registry.request_cancel(actor.task_id, "parent")
                actor.execution.thread_task.cancel()
                tasks.append(actor.execution.thread_task)
            actor.loop_task.cancel()
            tasks.append(actor.loop_task)
        await asyncio.gather(*tasks, return_exceptions=True)
        for actor in actors:
            actor.abandon()
            cancellation_registry.consume(actor.task_id)

    def close_sync(self) -> None:
        """不能 await 的依赖重配置路径：跨线程投递取消并清空登记，信号随之取走。"""
        self._closing = True
        actors = list(self._actors.values())
        self._actors.clear()
        for actor in actors:
            cancellation_registry.consume(actor.task_id)
            pending = [actor.loop_task]
            if actor.execution is not None:
                pending.append(actor.execution.thread_task)
            for task in pending:
                if task.done():
                    continue
                try:
                    task.get_loop().call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    log.warning(
                        "workflow_session_sync_close_cancel_failed",
                        extra={
                            "msg": "同步 reset 无法向 actor 所属事件循环投递取消",
                            "data": {"task_id": actor.task_id},
                        },
                    )

    def evict(self, actor: TaskActor) -> None:
        """actor 停下后注销自身；之后到达的信号会拉起新的 actor。"""
        if self._actors.get(actor.task_id) is actor:
            del self._actors[actor.task_id]

    def _actor(self, task_id: int) -> TaskActor:
        """取得或拉起 Task 的 actor；只能在事件循环线程调用。

        异常:
            RuntimeError: 会话已关闭。
        """
        if self._closing:
            raise RuntimeError("workflow session 已关闭")
        actor = self._actors.get(task_id)
        if actor is None:
            self._loop = asyncio.get_running_loop()
            actor = TaskActor(task_id, self)
            actor.loop_task.add_done_callback(_log_actor_exit)
            self._actors[task_id] = actor
        return actor

    async def _request(
            self, task_id: int, signal_type: Callable[..., Signal], *args: Any, **kwargs: Any
    ) -> Any:
        """构造一个带回执的信号发给 actor，并等 actor 兑现。

        调用方取消等待只会取消回执，不撤回已发出的信号：actor 照常处理，兑现时跳过已取消的回执。
        """
        receipt: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._actor(task_id).send(signal_type(*args, **kwargs, receipt=receipt))
        return await receipt

    async def _receipt(
            self, task_id: int, signal_type: Callable[..., Signal], *args: Any, **kwargs: Any
    ) -> SessionReceipt:
        return cast(SessionReceipt, await self._request(task_id, signal_type, *args, **kwargs))

    def _execution(self, task_id: int) -> Execution | None:
        actor = self._actors.get(task_id)
        return actor.execution if actor is not None else None
