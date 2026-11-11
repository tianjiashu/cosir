"""Assistant Transport 的应用服务：用户命令编排、Task 长订阅与 snapshot 读取。

本层把 wire 请求翻译成入队与命令，自己不做调度：是否新建、唤醒 Run、何时交接都由 Task 的
:class:`~app.core.task_actor.TaskActor` 监听外部队列后判定。消息一律经
``task_runtime_spaces.enqueue`` 入队（先确保 Task 的 actor 已拉起）；恢复与取消直接调用 actor。

错误契约：只抛领域异常，HTTP 映射归 API 层——``KeyError``（Task 不存在，404）、
``SessionConflictError``（状态冲突，409）、``UserInputRejectedError`` / ``ValueError``（输入或领域
校验失败）。attach 是跟随 Task 的长订阅，不触发业务执行。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.assistant_transport.request import DecisionRequest, SendMessageRequest
from app.assistant_transport.service.transport_stream_service import (
    AssistantTransportStreamService,
)
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.assistant_transport.state.snapshot_window import HistoryPage, window_snapshot
from app.config.logging.logger import log
from app.core.task_actor import SessionConflictError
from app.core.tool.background.background_registry import background_registry
from app.core.workflow.message_queue.task_message import (
    Delivery,
    RunLaunch,
    TaskMessage,
    UserInputBody,
)
from app.service import depends as service_depends
from app.storage.store_engines import main_session_factory
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class TransportAssistantService:
    """用户命令编排与订阅；SSE 编码与订阅生命周期委托 ``AssistantTransportStreamService``。"""

    def __init__(self) -> None:
        self._stream = AssistantTransportStreamService()
        self._tasks = service_depends.get_task_service()
        self._queue = service_depends.get_task_message_queue()
        self._inputs = service_depends.get_user_input_service()
        self._states = service_depends.get_conversation_task_state_service()

    # ── 命令 ──────────────────────────────────────────────────────────────

    async def send(self, task_id: int, request: SendMessageRequest) -> None:
        """发送：Task 有活动 Run（含挂起）时排队（``next_run``），空闲时立即处理（``immediate``）。

        排队消息在当前 Run 结束后一轮一条依次处理，用户可在队列里改为立即发送或删除；挂起时排队
        不唤醒 Run。空闲时用 ``immediate``：取消后队列里可能还留着旧的排队消息，新发送的这条应先跑
        （见 ``dispatch_policy.next_input``）。判定阶段与入队在 Task 闸门内完成，actor 不会在两者
        之间开出或落定 Run；执行者自行结束不经闸门，此时排队的消息由刚结束的 Run 收尾时接续。

        Agent 身份沿用 Task 当前 Run：Task 不绑定 Agent，身份随发起配置走，子 Agent / Team 节点的
        Task 不能因为用户在它的页面里发一条消息就退回 ``main_agent``。尚无 Run 时用 ``main_agent``。
        """
        current = await asyncio.to_thread(self._tasks.require_current_run, task_id)
        initial = (
            RunLaunch.from_fork(await asyncio.to_thread(self._tasks.get_task, task_id))
            if current is None
            else None
        )
        message = await asyncio.to_thread(
            self._inputs.prepare,
            task_id,
            request.to_run_command(),
            request.modelConfigId,
            request.reasoningEffort,
            current.agent_id
            if current is not None and current.agent_id
            else initial.agent_id
            if initial
            else "main_agent",
        )
        space = await asyncio.to_thread(task_runtime_spaces.get_or_create, task_id)
        async with space.async_operation():
            delivery: Delivery = "immediate" if await space.actor.phase() == "idle" else "next_run"
            await asyncio.to_thread(
                task_runtime_spaces.enqueue, task_id, replace(message, delivery=delivery)
            )

    async def send_queued_now(self, task_id: int, message_id: int) -> None:
        """把一条排队中的用户输入改为立即处理：撤回原行，以 ``immediate`` 重新入队（同一事务）。

        重新入队得到新的消息身份，actor 把它当作新到达的消息照常判定：有执行者就并入当前 Run，
        挂起就唤醒，空闲就以它开新 Run。不原地改 ``delivery``：actor 只按水位之后的新消息推进，
        原地更新唤不醒它。正文（含胶囊指针）、图片与发起配置原样沿用。

        异常:
            SessionConflictError: ``MESSAGE_CONSUMED``（消息已不在队列中，或不是用户输入）。
            KeyError: Task 不存在。
        """
        await asyncio.to_thread(self._send_queued_now, task_id, message_id)
        await asyncio.to_thread(self._states.refresh_queued_messages, task_id)

    def _send_queued_now(self, task_id: int, message_id: int) -> None:
        with main_session_factory().begin() as session:
            queued = self._withdraw_user_input(session, task_id, message_id)
            new_id = task_runtime_spaces.enqueue(
                task_id, TaskMessage.of(queued.body, delivery="immediate"), session=session
            )
        log.info(
            "queued_message_sent_now",
            extra={
                "msg": "排队消息已改为立即处理",
                "data": {"task_id": task_id, "message_id": message_id, "new_message_id": new_id},
            },
        )

    async def delete_queued(self, task_id: int, message_id: int) -> None:
        """删除一条排队中的用户输入。

        异常:
            SessionConflictError: ``MESSAGE_CONSUMED``（消息已不在队列中，或不是用户输入）。
            KeyError: Task 不存在。
        """
        await asyncio.to_thread(self._delete_queued, task_id, message_id)
        # 撤回不新增行、不唤醒 actor，排队投影由这里刷新。
        await asyncio.to_thread(self._states.refresh_queued_messages, task_id)

    def _delete_queued(self, task_id: int, message_id: int) -> None:
        with main_session_factory().begin() as session:
            self._withdraw_user_input(session, task_id, message_id)
        log.info(
            "queued_message_deleted",
            extra={"msg": "排队消息已删除", "data": {"task_id": task_id, "message_id": message_id}},
        )

    def _withdraw_user_input(self, session: Session, task_id: int, message_id: int) -> TaskMessage:
        """在调用方事务内撤回一条排队中的用户输入；不是用户输入时抛错，事务回滚、行保留。"""
        queued = self._queue.withdraw(session, task_id, message_id)
        if queued is None or not isinstance(queued.body, UserInputBody):
            raise SessionConflictError("MESSAGE_CONSUMED", "这条消息已不在队列中", retryable=False)
        return queued

    async def withdraw(self, task_id: int, message_id: int) -> None:
        """撤回最新一条已并入的用户输入开启的回合（语义见 ``TurnWithdrawalService``），不开新 Run。

        编辑不是后端命令：前端撤回后把正文（含胶囊指针）放回输入框，改完照常发送。
        只在 Task 静止（``idle``）时接受，不替用户停止：运行中的 Run 用的是 ``begin_run`` 时建好的
        内存上下文，看不到库里的移出，会继续回答被撤回的消息。前端只在 activity 为终态时开放入口，
        这里是最后一道守卫。静止看 actor 的执行阶段而不是 Run 状态：Run 落定终态后执行者还在收尾，
        此时改 context 会与它交错；判定在 Task 闸门内，与 actor 的推进互斥。

        异常:
            SessionConflictError: ``WITHDRAW_TARGET_STALE``（目标不是最新一条用户输入）、
                ``RUN_NOT_STOPPED``（Task 仍在运行、挂起或执行者尚在收尾）。
            KeyError: Task 不存在。
        """
        await asyncio.to_thread(self._tasks.get_task, task_id)
        space = await asyncio.to_thread(task_runtime_spaces.get_or_create, task_id)
        async with space.async_operation():
            if await space.actor.phase() != "idle":
                raise SessionConflictError("RUN_NOT_STOPPED", "当前运行尚未停止，请停止后再撤回")
            await asyncio.to_thread(
                service_depends.get_turn_withdrawal_service().withdraw, task_id, message_id
            )
        await asyncio.to_thread(self._republish, task_id)

    def _republish(self, task_id: int) -> None:
        """移出的行要从前端消失：从 canonical 记录重建并发布（同一临界区，不与投影交错）。"""
        self._states.republish_from_canonical(task_id)

    async def resume(self, task_id: int) -> None:
        """最新 Run 为 cancelled / failed 时从 prepare_input 续跑。

        异常:
            SessionConflictError: ``RUN_NOT_RESUMABLE``。
            KeyError: Task 不存在。
        """
        space = await asyncio.to_thread(task_runtime_spaces.get_or_create, task_id)
        await space.actor.resume()

    async def cancel(self, task_id: int) -> None:
        """用户停止 Task：交给 Task 的 actor（运行中打信号，挂起的 Run 直接迁移为 cancelled）。

        用户停止只打断当前驱动：后台工作、Agent Team 与子 Task 照常运行（见 ``TaskActor.cancel``），停它们
        走调用级的 :meth:`cancel_tool_call`。「用户已停止」通知由下一次驱动从结束原因推导。

        异常:
            KeyError: Task 不存在。
        """
        space = await asyncio.to_thread(task_runtime_spaces.get_or_create, task_id)
        await space.actor.cancel("user")

    def cancel_tool_call(self, task_id: int, tool_call_id: str) -> bool:
        """取消一次工具调用；没有可取消的对象（或已标记过）时返回 ``False``。

        这次调用登记了后台结果时撤销登记，不要求 Run 在执行（后台工作本来就在 Run 之外）：前台条目
        由撤销回调唤醒工具线程、以取消结果收尾；后台条目回投取消结论：有活动 Run 就交给它（不论
        登记它的是哪个 Run）；Task 空闲时先结算占位，结论留在队列里随下一次推进并入、不开新 Run。
        没有登记时只中止当前执行中的工具调用：看执行者而不是 Run 状态，挂起的 Run 没有执行者，标记了
        也没人取。

        异常:
            KeyError: Task 不存在。
        """
        self._tasks.get_task(task_id)
        return background_registry.request_cancel(
            task_id, tool_call_id, driving=task_runtime_spaces.get_or_create(task_id).actor.driving
        )

    def background_tool_call(self, task_id: int, tool_call_id: str) -> None:
        """把一次前台等待中的工具调用转到后台；结论以后作为 ``background_result`` 回投。

        异常:
            KeyError: Task 不存在。
            SessionConflictError: ``TOOL_CALL_NOT_DETACHABLE``（没有这次调用的前台等待）。
        """
        self._tasks.get_task(task_id)
        # 结论与转后台的先后由登记表在锁内裁决。
        if background_registry.detach(task_id, tool_call_id) != "detached":
            raise SessionConflictError(
                "TOOL_CALL_NOT_DETACHABLE", "这次工具调用已结束或不能转到后台", retryable=False
            )

    async def decide(self, task_id: int, tool_call_id: str, request: DecisionRequest) -> None:
        """用户对一次等待决定的工具调用作出决定：校验与入队归 ``ToolDecisionService``，
        不等工具执行。

        异常:
            KeyError: Task 不存在。
            SessionConflictError: ``DECISION_NOT_AWAITED`` / ``DECISION_ALREADY_SUBMITTED``。
            UserInputRejectedError: ``DECISION_DATA_INVALID``。
        """
        await asyncio.to_thread(self._tasks.get_task, task_id)
        await asyncio.to_thread(
            service_depends.get_tool_decision_service().submit,
            task_id,
            tool_call_id,
            request.decision,
            feedback=request.feedback,
            data=request.data,
        )

    # ── 订阅与读取 ────────────────────────────────────────────────────────

    def attach(self, task_id: int) -> StreamingResponse:
        """订阅 Task：先推 full 帧（Task 还没有 Run 也推），之后持续推送，跟随最新 Run 的变化。

        异常:
            KeyError: Task 不存在。
        """
        self._tasks.get_task(task_id)
        response = StreamingResponse(
            self._stream.stream_envelopes(task_id, lambda: False),
            media_type="text/event-stream",
        )
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Connection"] = "keep-alive"
        response.headers["X-Cosir-Task-Id"] = str(task_id)
        return response

    def get_state(self, task_id: int) -> ConversationStateSnapshot:
        """读取 Task 的 canonical snapshot（按历史窗口只带最近一段消息）。

        先校验 Task 存在：空 Task 与不存在的 Task 在投影层都表现为空消息列表，只能在这里区分。

        异常:
            KeyError: Task 不存在。
        """
        self._tasks.get_task(task_id)
        return window_snapshot(self._states.get_state(task_id))

    def get_history_page(self, task_id: int, end: int) -> HistoryPage:
        """完整历史中位置 ``end`` 之前的一页（前端往上翻时取）。

        异常:
            KeyError: Task 不存在。
            ValueError: ``end`` 来自已被替换的完整快照，页位置已失效。
        """
        self._tasks.get_task(task_id)
        return self._states.get_history_page(task_id, end)
