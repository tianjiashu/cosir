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
from typing import Literal, cast

from app.assistant_transport.request import DecisionRequest, SendMessageRequest
from app.assistant_transport.state.snapshot_window import HistoryPage
from app.assistant_transport.stream.session_sse_stream import SessionSseStream
from app.config.logging.logger import log
from app.core.tool.terminal_approval.registry import terminal_approvals
from app.core.workflow.cancel.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.workflow.message_queue.session_message import (
    RunLaunch,
    SessionMessage,
    UserInputBody,
)
from app.models.session.session_command_errors import SessionCommandConflictError
from app.models.terminal_permissions import TerminalPermissionMode
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
        """发送：默认立即处理——空闲开新 Run，运行中插话（下一次模型请求前并入，同批没开始的调用让位），
        挂起时唤醒；用户要求排队（``request.queue``）时有活动 Run 就以 ``next_run`` 排到它结束后，每个
        Run 一条依次处理，用户可在队列里立即发送或删除。判定规则在 ``SessionActor.user_input_delivery``，
        判定阶段与入队在 Session 闸门内完成，actor 不会在两者之间开出或落定 Run；执行者自行结束不经
        闸门，此时排队的消息由刚结束的 Run 收尾时接续。

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
            delivery = await space.actor.user_input_delivery(queue=request.queue)
            await asyncio.to_thread(
                session_runtime_spaces.enqueue,
                session_id,
                SessionMessage.of(body, delivery=delivery),
            )
            if delivery == "immediate":
                await space.actor.accept_immediate_input()

    def set_terminal_permission(self, session_id: int, mode: TerminalPermissionMode) -> None:
        """切换会话的终端权限（规则在 ``SessionService.set_terminal_permission_mode``）。"""
        self._sessions.set_terminal_permission_mode(session_id, mode)

    async def send_now(self, session_id: int, message_id: int) -> None:
        """「立即发送」：让一条还没并入的用户输入现在就被处理。

        排队（``next_run``）的原地改为 ``immediate``（``SessionMessageQueue.promote``，身份不变）；插话
        （``immediate``）的不改内容，两者都登记为待 actor 重新判定。随后在同一闸门内投递，并停下本 Session 当前执行中的
        工具调用：新输入已在 mailbox 里，tools_node 会跳过同批还没开始的调用，模型下一步就看到它。停止
        复用单停的信号，只对会轮询取消的工具即时生效（终端、浏览器、MCP 等），读文件、网页抓取这类没有
        取消点的照常跑完。没有执行中的调用（模型在输出、挂起、空闲、停止中）时不打断任何东西：这一行
        由 actor 当作待重判的消息照常判定——挂起被唤醒、空闲开新 Run（停止或失败后留下的插话也由此
        处理）、停止中等驱动落定后再判定，排队投影也随之刷新。

        异常:
            SessionCommandConflictError: ``MESSAGE_CONSUMED``（消息已不在队列中，或不是用户输入）。
            UserInputRejectedError: 这条输入的模型已从配置删除（``MODEL_CONFIG_UNAVAILABLE``）等，同发送。
            KeyError: Session 不存在。
        """
        space = await asyncio.to_thread(session_runtime_spaces.get_or_create, session_id)
        async with space.async_operation():
            await asyncio.to_thread(self._make_immediate, session_id, message_id)
            await space.actor.accept_immediate_input()
            interrupted = tool_call_cancellation_registry.running_call(session_id)
            if interrupted is not None and not self._cancel_call(session_id, interrupted):
                interrupted = None
            if interrupted is not None:
                # 停下了在途调用：这条输入打断过工作，之后再撤回要告诉模型（见 ``SessionActor.withdraw_input``）。
                await asyncio.to_thread(self._queue.mark_interrupted_work, session_id, [message_id])
        log.info(
            "queued_message_sent_now",
            extra={
                "msg": "用户输入改为立即处理",
                "data": {
                    "session_id": session_id,
                    "message_id": message_id,
                    "interrupted_call_id": interrupted,
                },
            },
        )

    def _make_immediate(self, session_id: int, message_id: int) -> None:
        # 先认 Session：队列对不存在的 Session 只是「没有这一行」，会被说成 MESSAGE_CONSUMED 而不是 404。
        self._sessions.get_session(session_id)
        with main_db_factory().begin() as db:
            promoted = self._queue.promote(db, session_id, message_id)
            if promoted is None:
                raise SessionCommandConflictError(
                    "MESSAGE_CONSUMED", "这条消息已不在队列中", retryable=False
                )
            # 与发送同一道准入：模型已删除的输入开不了 Run（调度会跳过它），在这里拒绝、事务回滚，
            # 用户才知道要编辑或删除它，而不是点了没反应。
            launch = cast(UserInputBody, promoted.body).launch
            service_depends.get_model_config_service().check_launch_config(
                launch.model_config_id, launch.reasoning_effort
            )

    async def delete_queued(self, session_id: int, message_id: int) -> None:
        """删除一条还没并入的用户输入（排队的或插话，编辑也经这里移出）。

        在 Session 闸门内交给 actor（``SessionActor.withdraw_input``）：投递也在闸门内，删除与投递因此
        不会交错，mailbox 里不会留下已删除输入的副本。

        异常:
            SessionCommandConflictError: ``MESSAGE_CONSUMED``（消息已不在队列中，或不是用户输入）。
            KeyError: Session 不存在。
        """
        # 先认 Session：队列对不存在的 Session 只是「没有这一行」，会被说成 MESSAGE_CONSUMED 而不是 404。
        await asyncio.to_thread(self._sessions.get_session, session_id)
        space = await asyncio.to_thread(session_runtime_spaces.get_or_create, session_id)
        async with space.async_operation():
            await asyncio.to_thread(space.actor.withdraw_input, message_id)
        # 用户输入本来就投影在排队区，删除后由这里刷新（可能附带的撤回通知不投影）。
        await asyncio.to_thread(self._states.refresh_queued_messages, session_id)

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
                raise SessionCommandConflictError(
                    "RUN_NOT_STOPPED", "当前运行尚未停止，请停止后再撤回"
                )
            await asyncio.to_thread(
                service_depends.get_turn_withdrawal_service().withdraw, session_id, message_id
            )
        # 移出的行要从前端消失：按 canonical 记录重建并发布整份快照。
        await asyncio.to_thread(self._states.republish_from_canonical, session_id)

    async def decide_terminal(
        self, session_id: int, request_id: str, decision: Literal["approve", "reject"]
    ) -> None:
        """与新输入共用展示会话闸门，直接裁决原调用；不入队、不新开 Run。"""
        space = await asyncio.to_thread(session_runtime_spaces.get_or_create, session_id)
        async with space.async_operation():
            # 入队成功后的 HTTP 调用可能已断开；批准同样先交给 Actor 投递已提交输入。
            await space.actor.accept_immediate_input()
            await asyncio.to_thread(terminal_approvals.decide, session_id, request_id, decision)

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
        # 只撤本会话自己发起的：用户停止不动子 Session，子 Agent 显示在这里的批准留给用户处理。
        terminal_approvals.cancel_originated(session_id, "stopped")
        await space.actor.cancel("user")

    def cancel_tool_call(self, session_id: int, tool_call_id: str) -> None:
        """取消一次工具调用。

        这次调用登记了后台结果时撤销登记，不要求 Run 在执行（后台工作本来就在 Run 之外）：前台条目
        由撤销回调唤醒工具线程、以取消结果收尾；后台条目回投取消结论：有活动 Run 就交给它（不论
        登记它的是哪个 Run）；Session 空闲时先结算占位，结论留在队列里随下一次推进并入、不开新 Run。
        没有登记时只中止本 Session 当前执行中的那次调用（执行器登记）：没开始、已结束或不存在的调用
        不受理，挂起的 Run 没有执行者，自然也没有执行中的调用。

        异常:
            KeyError: Session 不存在。
            SessionCommandConflictError: ``TOOL_CALL_NOT_CANCELLABLE``（没有可取消的对象：调用已结束、
                已标记过取消、还没开始或不存在）。
        """
        self._sessions.get_session(session_id)
        if not self._cancel_call(session_id, tool_call_id):
            raise SessionCommandConflictError(
                "TOOL_CALL_NOT_CANCELLABLE", "这次工具调用已结束或已在取消", retryable=False
            )

    @staticmethod
    def _cancel_call(session_id: int, tool_call_id: str) -> bool:
        """单停一次调用：先撤它的终端审批，再停后台作业或当前执行中的调用；没有可停的返回 False。"""
        return terminal_approvals.cancel_call(
            session_id, tool_call_id
        ) or command_results.request_cancel(session_id, tool_call_id)

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
