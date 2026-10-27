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
from typing import cast

from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from app.assistant_transport.request import (
    DecisionRequest,
    EditQueuedMessageRequest,
    RerunMessageRequest,
    SendMessageRequest,
)
from app.assistant_transport.service.transport_stream_service import (
    AssistantTransportStreamService,
)
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.config.configuration import get_tool_registry
from app.config.logging.logger import log
from app.core.task_actor import SessionConflictError
from app.core.tool.schemas import ToolDefinition
from app.core.workflow.cancel.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.workflow.inbox.task_message import DecisionBody, TaskMessage, UserInputBody
from app.models.errors.input_errors import UserInputRejectedError
from app.models.message_metadata import UserMessageMetadata
from app.service import depends as service_depends
from app.storage.store_engines import main_session_factory
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

# 编辑重跑等当前执行停下：取消在下一个检查点生效、工具也响应取消信号，正常远小于上限。
_RERUN_STOP_WAIT_SECONDS = 30.0


class TransportAssistantService:
    """用户命令编排与订阅；SSE 编码与订阅生命周期委托 ``AssistantTransportStreamService``。"""

    def __init__(self) -> None:
        self._stream = AssistantTransportStreamService()
        self._tasks = service_depends.get_task_service()
        self._queue = service_depends.get_task_message_queue()
        self._inputs = service_depends.get_user_input_service()
        self._states = service_depends.get_conversation_task_state_service()
        self._run = service_depends.get_conversation_run_ledger()
        self._context = service_depends.get_conversation_task_context_service()

    # ── 命令 ──────────────────────────────────────────────────────────────

    async def send(self, task_id: int, request: SendMessageRequest) -> None:
        """发送：有活动 Run 时按 ``delivery`` 立即并入或排到本轮之后，否则新开 Run。

        Agent 身份沿用 Task 当前 Run：Task 不绑定 Agent，身份随发起配置走，子 Agent / Team 节点的
        Task 不能因为用户在它的页面里发一条消息就退回 ``main_agent``。尚无 Run 时用 ``main_agent``。
        """
        current = await asyncio.to_thread(self._tasks.require_current_run, task_id)
        message = await asyncio.to_thread(
            self._inputs.prepare,
            task_id,
            request.to_run_command(),
            request.modelConfigId,
            request.reasoningEffort,
            current.agent_id if current is not None and current.agent_id else "main_agent",
            delivery=request.delivery,
        )
        await asyncio.to_thread(task_runtime_spaces.enqueue, task_id, message)

    async def edit_queued(
            self, task_id: int, message_id: int, request: EditQueuedMessageRequest
    ) -> None:
        """原地编辑一条排队中的用户输入：身份、``delivery`` 与发起配置都不变，只改正文与附件。

        不经 actor：这是对队列行的条件更新，与 ``prepare_input`` 的消费确认由「行仍存在才更新」
        仲裁，与 Run 状态无关。按原配置的模型校验能力（图片、推理强度），因为消息最终在那个模型
        上运行。已投递进 mailbox 的旧副本无需撤回：``prepare_input`` 并入时以确认删除返回的行内容
        为准。消息已被并入时不会转成重跑。

        异常:
            SessionConflictError: ``MESSAGE_CONSUMED``（消息已不在队列中，或不是用户输入）。
            UserInputRejectedError / ValueError: 输入不合法。
            KeyError: Task 不存在。
        """
        queued = await asyncio.to_thread(self._queue.find, task_id, message_id)
        if queued is None or not isinstance(queued.body, UserInputBody):
            raise _message_consumed()
        launch = queued.body.launch
        # 复用原消息的展示绑定：编辑请求里的附件可能只剩 token，路径从原消息恢复。
        prepared = await asyncio.to_thread(
            self._inputs.prepare,
            task_id,
            request.to_run_command(),
            launch.model_config_id,
            launch.options.reasoning_effort,
            launch.agent_id,
            reuse=queued.body.presentation(),
        )
        # 发起配置原样沿用：``prepare`` 按请求重组了 launch（禁用工具为空），这里换回原值。
        body = cast(UserInputBody, prepared.body).model_copy(update={"launch": launch})
        edited = TaskMessage.of(body, delivery=queued.delivery)
        if not await asyncio.to_thread(self._queue.update, task_id, message_id, edited):
            raise _message_consumed()
        # 原地更新不新增行、不唤醒 actor，排队投影由这里刷新；消费投影搬移的就是这份新内容。
        await asyncio.to_thread(self._states.refresh_queued_messages, task_id)

    async def rerun(self, task_id: int, message_id: int, request: RerunMessageRequest) -> None:
        """编辑最新一条已并入的用户输入并重跑：停下当前 Run → 软删除它及其后 context + 以新消息
        入队（同一事务）→ 由 Task 的 actor 照常开新 Run。

        动任何数据之前先校验目标并解析新输入：非法输入同步 400，原消息不受影响。等停下看 actor 的
        执行阶段而不是 Run 状态：Run 落定终态后执行者还在收尾，此时改 context 会与它交错。最后一步
        在 Task 闸门内复查阶段——拿闸门之前 actor 可能已开出新 Run，闸门与 actor 的推进互斥。Run
        只会前进：旧 Run 不重置，被移出的行保留供观测页查看。外部工具副作用不回滚。

        异常:
            SessionConflictError: ``EDIT_TARGET_STALE``（目标不是最新一条用户输入）、
                ``RUN_NOT_STOPPED``（等停止超时，或停下后又开出了新 Run）。
            UserInputRejectedError / ValueError: 新输入不合法。
            KeyError: Task 不存在。
        """
        current = await asyncio.to_thread(self._tasks.require_current_run, task_id)
        target = await asyncio.to_thread(self._context.latest_user_input, task_id)
        if target is None or target.message_id != message_id:
            raise SessionConflictError(
                "EDIT_TARGET_STALE", "只能编辑最新一条用户消息，请刷新后重试"
            )
        # 复用原消息的展示绑定：页面重载后前端只剩附件 token，路径只能从原消息恢复。
        message = await asyncio.to_thread(
            self._inputs.prepare,
            task_id,
            request.to_run_command(),
            request.modelConfigId,
            request.reasoningEffort,
            current.agent_id if current is not None and current.agent_id else "main_agent",
            reuse=cast(UserMessageMetadata, target.metadata).presentation,
        )
        space = await asyncio.to_thread(task_runtime_spaces.get_or_create, task_id)
        # 停止交给 actor：运行中打信号、挂起的直接迁移为 cancelled、静止时什么也不做。``edited``
        # 不留「用户已取消」通知——被打断的内容随后就被移出。
        await space.actor.cancel("edited")
        if not await space.actor.wait_idle(_RERUN_STOP_WAIT_SECONDS):
            raise _run_not_stopped()
        async with space.async_operation():
            if await space.actor.phase() != "idle":
                raise _run_not_stopped()
            await asyncio.to_thread(self._replace_latest_input, task_id, message_id, message)
        await asyncio.to_thread(self._republish, task_id)

    def _replace_latest_input(self, task_id: int, message_id: int, message: TaskMessage) -> None:
        """调用方持 Task 闸门且已确认静止：复查目标，软删除它及其后 context 并让新消息入队，
        同一事务提交。"""
        with main_session_factory().begin() as session:
            target = self._context.latest_user_input(task_id, session=session)
            if target is None or target.message_id != message_id:
                raise SessionConflictError(
                    "EDIT_TARGET_STALE", "只能编辑最新一条用户消息，请刷新后重试"
                )
            self._context.exclude_from(task_id, target.sequence, session)
            task_runtime_spaces.enqueue(task_id, message, session=session)

    def _republish(self, task_id: int) -> None:
        """移出的行要从前端消失：从 canonical 记录重建并发布快照。"""
        self._states.publish_state(task_id, self._states.rebuild_state(task_id))

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

        向 Agent Team 与子 Task 的传播、「用户已取消」通知都在 Run 落定 cancelled 后由 actor 完成，
        这里不提前做：此刻 Run 还没停下，也可能根本没有可停止的对象。

        异常:
            KeyError: Task 不存在。
        """
        space = await asyncio.to_thread(task_runtime_spaces.get_or_create, task_id)
        await space.actor.cancel("user")

    def cancel_tool_call(self, task_id: int, tool_call_id: str) -> bool:
        """只中止当前执行中的一次工具调用；没有执行者或已标记过时返回 ``False``。

        看执行者而不是 Run 状态：挂起的 Run 没有执行者，工具调用都已闭合，标记了也没人取。

        异常:
            KeyError: Task 不存在。
        """
        self._tasks.get_task(task_id)
        if task_runtime_spaces.get_or_create(task_id).actor.driving:
            return tool_call_cancellation_registry.mark_cancelled(task_id, tool_call_id)
        return False

    async def decide(self, task_id: int, tool_call_id: str, request: DecisionRequest) -> None:
        """对一次等待用户决定的工具调用作出决定：通用校验后入队 ``DecisionBody``，不等工具执行。

        只校验能同步回答用户、且对所有 HITL 工具都成立的事实：调用的等待占位仍为 ``pending``
        （事实源是 context，不读 TeamRun 之类的工具业务事实）、队列里没有它未处理的决定、载荷符合
        工具声明的 ``decision_model``。工具自己的业务状态在执行决定时由工具判定（如方案已被取消时
        以失败结果收尾）。决定之后去哪由 ``ToolDefinition.user_decision`` 决定，这里不关心。

        异常:
            KeyError: Task 不存在。
            SessionConflictError: ``DECISION_NOT_AWAITED`` / ``DECISION_ALREADY_SUBMITTED``。
            UserInputRejectedError: ``DECISION_DATA_INVALID``。
        """
        await asyncio.to_thread(self._tasks.get_task, task_id)
        tool_name = await asyncio.to_thread(
            self._context.awaiting_decision_tool, task_id, tool_call_id
        )
        if tool_name is None:
            raise SessionConflictError("DECISION_NOT_AWAITED", "该操作已不在等待决定，请刷新后重试")
        pending = await asyncio.to_thread(self._queue.pending_nowait, task_id)
        if any(
            isinstance(message.body, DecisionBody) and message.body.tool_call_id == tool_call_id
            for message in pending
        ):
            raise SessionConflictError("DECISION_ALREADY_SUBMITTED", "该决定已提交，正在处理")
        # 等待占位只会由已注册的 HITL 工具写入，定义必然存在。
        definition = cast(ToolDefinition, get_tool_registry().get_tool_definition(tool_name))
        decision_model = definition.decision_model
        data = None
        if request.decision == "approve" and decision_model is not None:
            try:
                data = decision_model.model_validate(request.data or {}).model_dump(mode="json")
            except ValidationError as exc:
                raise UserInputRejectedError(
                    "DECISION_DATA_INVALID", "提交的内容不合法，请检查后重试", detail=str(exc)
                ) from exc
        feedback = request.feedback.strip() if request.feedback else None
        body = DecisionBody(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            decision=request.decision,
            feedback=feedback or None,
            data=data,
        )
        await asyncio.to_thread(task_runtime_spaces.enqueue, task_id, TaskMessage.of(body))
        log.info(
            "tool_decision_enqueued",
            extra={
                "msg": "用户决定已入队，等待工具执行或交给模型",
                "data": {
                    "task_id": task_id,
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "decision": request.decision,
                },
            },
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
        """读取 Task 的 canonical snapshot。

        先校验 Task 存在：空 Task 与不存在的 Task 在投影层都表现为空消息列表，只能在这里区分。

        异常:
            KeyError: Task 不存在。
        """
        self._tasks.get_task(task_id)
        return self._states.get_state(task_id)


def _run_not_stopped() -> SessionConflictError:
    return SessionConflictError("RUN_NOT_STOPPED", "当前运行尚未停止，请稍后重试")


def _message_consumed() -> SessionConflictError:
    return SessionConflictError(
        "MESSAGE_CONSUMED", "这条消息已开始处理，未作修改", retryable=False
    )
