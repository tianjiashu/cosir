"""Assistant Transport 的应用服务：用户命令编排、Session 长订阅与历史分页。

本层把 wire 请求翻译成入队与命令，自己不做调度：是否新建、唤醒 Run、何时交接都由 Session 的
:class:`~app.core.session_actor.SessionActor` 监听外部队列后判定。消息一律经
``session_runtime_spaces.enqueue`` 入队（先确保 Session 的 actor 已拉起）；恢复与取消直接调用 actor。

错误契约：只抛领域异常，HTTP 映射归 API 层——``KeyError``（Session 不存在，404）、
``SessionCommandConflictError``（状态冲突，409）、``UserInputRejectedError`` / ``ValueError``（输入或领域
校验失败）。每个命令先认 Session（``get_session``），「Session 不存在」不会被说成别的冲突。attach 是跟随 Session
的长订阅，不触发业务执行。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from sqlalchemy.orm import Session as DbSession

from app.assistant_transport.request import DecisionRequest, SendMessageRequest
from app.assistant_transport.state.snapshot_window import HistoryPage
from app.assistant_transport.stream.session_sse_stream import SessionSseStream
from app.config.logging.logger import log
from app.core.workflow.message_queue.dispatch_policy import send_delivery
from app.core.workflow.message_queue.session_message import (
    RunLaunch,
    SessionMessage,
    UserInputBody,
)
from app.models.errors.session_command_errors import SessionCommandConflictError
from app.service import depends as service_depends
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces
from app.storage.store_engines import main_db_factory
from app.tools.tool_handler.terminal.command_results import command_results


class AssistantTransportService:
    """用户命令编排与订阅；订阅的连接生命周期与 SSE 编码在 ``SessionSseStream``。"""

    def __init__(self) -> None:
        self._sessions = service_depends.get_session_service()
        self._queue = service_depends.get_session_message_queue()
        self._inputs = service_depends.get_user_input_service()
        self._states = service_depends.get_conversation_state_service()
        self._stream = SessionSseStream(self._states)

    # ── 命令 ──────────────────────────────────────────────────────────────

    async def send(self, session_id: int, request: SendMessageRequest) -> None:
        """发送：Session 有活动 Run（含挂起）时排队（``next_run``），空闲时立即处理（``immediate``）；
        挂起的 Run 在等用户回答提问时也立即处理——消息唤醒它，提问作废（规则见
        ``dispatch_policy.send_delivery``）。

        排队消息在当前 Run 结束后每个 Run 一条依次处理，用户可在队列里改为立即发送或删除；只在等
        后台结果的挂起，排队不唤醒 Run。判定阶段与入队在 Session 闸门内完成，actor 不会在两者
        之间开出或落定 Run；执行者自行结束不经闸门，此时排队的消息由刚结束的 Run 收尾时接续。

        Agent 身份沿用 Session 当前 Run：Session 不绑定 Agent，身份随发起配置走，子 Agent 的 Session
        不能因为用户在它的页面里发一条消息就退回 ``main_agent``。尚无 Run 时用 ``main_agent``。
        """
        current = await asyncio.to_thread(self._sessions.require_current_run, session_id)
        initial = (
            RunLaunch.from_fork(await asyncio.to_thread(self._sessions.get_session, session_id))
            if current is None
            else None
        )
        body = await asyncio.to_thread(
            self._inputs.prepare,
            session_id,
            request.to_run_command(),
            request.modelConfigId,
            request.reasoningEffort,
            current.agent_id
            if current is not None and current.agent_id
            else initial.agent_id
            if initial
            else "main_agent",
        )
        space = await asyncio.to_thread(session_runtime_spaces.get_or_create, session_id)
        async with space.async_operation():
            phase = await space.actor.phase()
            delivery = send_delivery(
                phase,
                awaits_user_reply=phase == "suspended"
                and await asyncio.to_thread(
                    service_depends.get_tool_decision_service().awaits_user_reply, session_id
                ),
            )
            await asyncio.to_thread(
                session_runtime_spaces.enqueue, session_id, SessionMessage.of(body, delivery=delivery)
            )

    async def send_queued_now(self, session_id: int, message_id: int) -> None:
        """把一条排队中的用户输入改为立即处理：撤回原行，以 ``immediate`` 重新入队（同一事务）。

        重新入队得到新的消息身份，actor 把它当作新到达的消息照常判定：有执行者就并入当前 Run，
        挂起就唤醒，空闲就以它开新 Run。不原地改 ``delivery``：actor 只按水位之后的新消息推进，
        原地更新唤不醒它。正文（含胶囊指针）、图片与发起配置原样沿用。

        异常:
            SessionCommandConflictError: ``MESSAGE_CONSUMED``（消息已不在队列中，或不是用户输入）。
            KeyError: Session 不存在。
        """
        await asyncio.to_thread(self._send_queued_now, session_id, message_id)
        await asyncio.to_thread(self._states.refresh_queued_messages, session_id)

    def _send_queued_now(self, session_id: int, message_id: int) -> None:
        # 先认 Session：队列对不存在的 Session 只是「没有这一行」，会被说成 MESSAGE_CONSUMED 而不是 404。
        self._sessions.get_session(session_id)
        with main_db_factory().begin() as db:
            queued = self._withdraw_user_input(db, session_id, message_id)
            new_id = session_runtime_spaces.enqueue(
                session_id, SessionMessage.of(queued.body, delivery="immediate"), db=db
            )
        log.info(
            "queued_message_sent_now",
            extra={
                "msg": "排队消息已改为立即处理",
                "data": {"session_id": session_id, "message_id": message_id, "new_message_id": new_id},
            },
        )

    async def delete_queued(self, session_id: int, message_id: int) -> None:
        """删除一条排队中的用户输入。

        异常:
            SessionCommandConflictError: ``MESSAGE_CONSUMED``（消息已不在队列中，或不是用户输入）。
            KeyError: Session 不存在。
        """
        await asyncio.to_thread(self._delete_queued, session_id, message_id)
        # 撤回不新增行、不唤醒 actor，排队投影由这里刷新。
        await asyncio.to_thread(self._states.refresh_queued_messages, session_id)

    def _delete_queued(self, session_id: int, message_id: int) -> None:
        self._sessions.get_session(session_id)
        with main_db_factory().begin() as db:
            self._withdraw_user_input(db, session_id, message_id)
        log.info(
            "queued_message_deleted",
            extra={"msg": "排队消息已删除", "data": {"session_id": session_id, "message_id": message_id}},
        )

    def _withdraw_user_input(self, db: DbSession, session_id: int, message_id: int) -> SessionMessage:
        """在调用方事务内撤回一条排队中的用户输入；不是用户输入时抛错，事务回滚、行保留。"""
        queued = self._queue.withdraw(db, session_id, message_id)
        if queued is None or not isinstance(queued.body, UserInputBody):
            raise SessionCommandConflictError("MESSAGE_CONSUMED", "这条消息已不在队列中", retryable=False)
        return queued

    async def withdraw(self, session_id: int, message_id: int) -> None:
        """撤回最新一条已并入的用户输入开启的回合（语义见 ``TurnWithdrawalService``），不开新 Run。

        编辑不是后端命令：前端撤回后把正文（含胶囊指针）放回输入框，改完照常发送。
        只在 Session 静止（``idle``）时接受，不替用户停止：运行中的 Run 用的是 ``begin_run`` 时建好的
        内存上下文，看不到库里的移出，会继续回答被撤回的消息。前端只在 activity 为终态时开放入口，
        这里是最后一道守卫。静止看 actor 的执行阶段而不是 Run 状态：Run 落定终态后执行者还在收尾，
        此时改 context 会与它交错；判定在 Session 闸门内，与 actor 的推进互斥。

        异常:
            SessionCommandConflictError: ``WITHDRAW_TARGET_STALE``（目标不是最新一条用户输入）、
                ``RUN_NOT_STOPPED``（Session 仍在运行、挂起或执行者尚在收尾）。
            KeyError: Session 不存在。
        """
        await asyncio.to_thread(self._sessions.get_session, session_id)
        space = await asyncio.to_thread(session_runtime_spaces.get_or_create, session_id)
        async with space.async_operation():
            if await space.actor.phase() != "idle":
                raise SessionCommandConflictError("RUN_NOT_STOPPED", "当前运行尚未停止，请停止后再撤回")
            await asyncio.to_thread(
                service_depends.get_turn_withdrawal_service().withdraw, session_id, message_id
            )
        # 移出的行要从前端消失：按 canonical 记录重建并发布整份快照。
        await asyncio.to_thread(self._states.republish_from_canonical, session_id)

    async def resume(self, session_id: int) -> None:
        """最新 Run 为 cancelled / failed 时从 prepare_input 续跑。

        异常:
            SessionCommandConflictError: ``RUN_NOT_RESUMABLE``。
            KeyError: Session 不存在。
        """
        await asyncio.to_thread(self._sessions.get_session, session_id)
        space = await asyncio.to_thread(session_runtime_spaces.get_or_create, session_id)
        await space.actor.resume()

    async def cancel(self, session_id: int) -> None:
        """用户停止 Session：交给 Session 的 actor（运行中打信号，挂起的 Run 直接迁移为 cancelled）。

        用户停止只打断当前驱动：后台工作与子 Session 照常运行（见 ``SessionActor.cancel``）。停后台命令走
        调用级的 :meth:`cancel_tool_call`，停子 Session 就是对它本身发停止。「用户已停止」通知由下一次驱动
        从结束原因推导。

        异常:
            KeyError: Session 不存在。
        """
        await asyncio.to_thread(self._sessions.get_session, session_id)
        space = await asyncio.to_thread(session_runtime_spaces.get_or_create, session_id)
        await space.actor.cancel("user")

    def cancel_tool_call(self, session_id: int, tool_call_id: str) -> None:
        """取消一次工具调用。

        这次调用登记了后台结果时撤销登记，不要求 Run 在执行（后台工作本来就在 Run 之外）：前台条目
        由撤销回调唤醒工具线程、以取消结果收尾；后台条目回投取消结论：有活动 Run 就交给它（不论
        登记它的是哪个 Run）；Session 空闲时先结算占位，结论留在队列里随下一次推进并入、不开新 Run。
        没有登记时只中止当前执行中的工具调用：看执行者而不是 Run 状态，挂起的 Run 没有执行者，标记了
        也没人取。

        异常:
            KeyError: Session 不存在。
            SessionCommandConflictError: ``TOOL_CALL_NOT_CANCELLABLE``（没有可取消的对象：调用已结束、
                已标记过取消，或 Session 当前没有执行者）。
        """
        self._sessions.get_session(session_id)
        driving = session_runtime_spaces.get_or_create(session_id).actor.driving
        if not command_results.request_cancel(session_id, tool_call_id, driving=driving):
            raise SessionCommandConflictError(
                "TOOL_CALL_NOT_CANCELLABLE", "这次工具调用已结束或已在取消", retryable=False
            )

    def background_tool_call(self, session_id: int, tool_call_id: str) -> None:
        """把一次前台等待中的工具调用转到后台；结论以后作为 ``background_result`` 回投。

        异常:
            KeyError: Session 不存在。
            SessionCommandConflictError: ``TOOL_CALL_NOT_DETACHABLE``（没有这次调用的前台等待）。
        """
        self._sessions.get_session(session_id)
        # 结论与转后台的先后由登记表在锁内裁决。
        if command_results.detach(session_id, tool_call_id) != "detached":
            raise SessionCommandConflictError(
                "TOOL_CALL_NOT_DETACHABLE", "这次工具调用已结束或不能转到后台", retryable=False
            )

    async def decide(self, session_id: int, tool_call_id: str, request: DecisionRequest) -> None:
        """用户对一次等待决定的工具调用作出决定：校验与入队归 ``ToolDecisionService``，
        不等工具执行。

        异常:
            KeyError: Session 不存在。
            SessionCommandConflictError: ``DECISION_NOT_AWAITED`` / ``DECISION_ALREADY_SUBMITTED``。
            UserInputRejectedError: ``DECISION_DATA_INVALID``（载荷不符合工具声明的形状）。
        """
        await asyncio.to_thread(self._sessions.get_session, session_id)
        await asyncio.to_thread(
            service_depends.get_tool_decision_service().submit,
            session_id,
            tool_call_id,
            request.decision,
            feedback=request.feedback,
            data=request.data,
        )

    # ── 订阅与历史分页 ────────────────────────────────────────────────────────

    def attach(self, session_id: int) -> AsyncIterator[str]:
        """订阅 Session 的 SSE 记录：先推 full 帧（Session 还没有 Run 也推），之后持续推送，跟随最新 Run 的变化。

        异常:
            KeyError: Session 不存在（在开始流之前同步抛出，API 层据此回 404）。
        """
        self._sessions.get_session(session_id)
        return self._stream.sse_records(session_id)

    def get_history_page(self, session_id: int, end: int) -> HistoryPage:
        """完整历史中位置 ``end`` 之前的一页（前端往上翻时取）。

        异常:
            KeyError: Session 不存在。
            ValueError: ``end`` 来自已被替换的完整快照，页位置已失效。
        """
        self._sessions.get_session(session_id)
        return self._states.get_history_page(session_id, end)
