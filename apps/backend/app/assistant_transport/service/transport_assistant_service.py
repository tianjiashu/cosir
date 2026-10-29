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
from app.core.task_actor import SessionConflictError
from app.core.tool.background import placeholders
from app.core.tool.background.deferred_registry import deferred_registry
from app.core.workflow.cancel.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.workflow.message_queue.task_message import (
    NoticeBody,
    TaskMessage,
    UserInputBody,
)
from app.models.message_metadata import UserMessageMetadata
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
            raise SessionConflictError(
                "MESSAGE_CONSUMED", "这条消息已开始处理，未作修改", retryable=False
            )
        launch = queued.body.launch
        # 复用原消息的展示绑定：编辑请求里的附件可能只剩 token，路径从原消息恢复。
        prepared = await asyncio.to_thread(
            self._inputs.prepare,
            task_id,
            request.to_run_command(),
            launch.model_config_id,
            launch.options.reasoning_effort,
            launch.agent_id,
            reuse=queued.body.presentation,
        )
        # 发起配置原样沿用：``prepare`` 按请求重组了 launch（禁用工具为空），这里换回原值。
        body = cast(UserInputBody, prepared.body).model_copy(update={"launch": launch})
        edited = TaskMessage.of(body, delivery=queued.delivery)
        if not await asyncio.to_thread(self._queue.update, task_id, message_id, edited):
            raise SessionConflictError(
                "MESSAGE_CONSUMED", "这条消息已开始处理，未作修改", retryable=False
            )
        # 原地更新不新增行、不唤醒 actor，排队投影由这里刷新；消费投影搬移的就是这份新内容。
        await asyncio.to_thread(self._states.refresh_queued_messages, task_id)

    async def rerun(self, task_id: int, message_id: int, request: RerunMessageRequest) -> None:
        """编辑最新一条已并入的用户输入并重跑：软删除它及其后 context + 以新消息入队（同一事务）→
        由 Task 的 actor 照常开新 Run。

        只在 Task 静止（``idle``）时接受，不替用户停止：运行中的 Run 用的是 ``begin_run`` 时建好的
        内存上下文，看不到库里的移出，会继续回答旧消息并把结果写在移出区段之后（工具结果还会失去
        配对的调用），新消息也会被并进这个旧 Run。前端只在 activity 为终态时开放入口，这里是最后一道
        守卫。静止看 actor 的执行阶段而不是 Run 状态：Run 落定终态后执行者还在收尾，此时改 context
        会与它交错；判定在 Task 闸门内，与 actor 的推进互斥。

        动任何数据之前先校验目标并解析新输入：非法输入同步 400，原消息不受影响。Run 只会前进：旧 Run
        不重置，被移出的行保留供观测页查看。外部工具副作用不回滚。

        异常:
            SessionConflictError: ``EDIT_TARGET_STALE``（目标不是最新一条用户输入）、
                ``RUN_NOT_STOPPED``（Task 仍在运行、挂起或执行者尚在收尾）。
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
        async with space.async_operation():
            if await space.actor.phase() != "idle":
                raise SessionConflictError("RUN_NOT_STOPPED", "当前运行尚未停止，请停止后再编辑重跑")
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
        """取消一次工具调用；没有可取消的对象（或已标记过）时返回 ``False``。

        这次调用登记了延迟结果时撤销登记，不要求 Run 在执行（后台工作本来就在 Run 之外）：前台条目
        由撤销回调唤醒工具线程、以取消结果收尾；后台条目把占位行推到 ``cancelled``，并给模型留一条
        不唤醒的通知——用户主动取消即接管，Agent 下次推进时一并读到。没有登记时只中止当前执行中
        的工具调用：看执行者而不是 Run 状态，挂起的 Run 没有执行者，标记了也没人取。

        异常:
            KeyError: Task 不存在。
        """
        self._tasks.get_task(task_id)
        if deferred_registry.owns(task_id, tool_call_id):
            return self._cancel_deferred(task_id, tool_call_id)
        if task_runtime_spaces.get_or_create(task_id).actor.driving:
            return tool_call_cancellation_registry.mark_cancelled(task_id, tool_call_id)
        return False

    def _cancel_deferred(self, task_id: int, tool_call_id: str) -> bool:
        dropped = deferred_registry.cancel(tool_call_id)
        if dropped is None:
            # 与完成或 Run 停止撤销竞争失败：条目已结束，没有可取消的对象。
            return False
        if dropped.mode == "background":
            placeholders.settle_placeholders(
                task_id, [tool_call_id], "cancelled", placeholders.USER_CANCELLED_HINT
            )
            task_runtime_spaces.enqueue(
                task_id,
                TaskMessage.of(
                    NoticeBody(
                        content=_DEFERRED_CANCELLED_NOTICE.format(tool_call_id=tool_call_id),
                        notice_kind="deferred_result_cancelled",
                    )
                ),
            )
        return True

    def background_tool_call(self, task_id: int, tool_call_id: str) -> None:
        """把一次前台等待中的工具调用转到后台；结论以后作为 ``background_result`` 回投。

        异常:
            KeyError: Task 不存在。
            SessionConflictError: ``TOOL_CALL_NOT_DETACHABLE``（没有这次调用的前台等待）。
        """
        self._tasks.get_task(task_id)
        # 先按 Task 寻址，避免转走别的 Task 的调用；结论与转后台的先后由登记表在锁内裁决。
        if (
                not deferred_registry.owns(task_id, tool_call_id)
                or deferred_registry.detach(tool_call_id) != "detached"
        ):
            raise SessionConflictError(
                "TOOL_CALL_NOT_DETACHABLE", "这次工具调用已结束或不能转到后台", retryable=False
            )

    async def decide(self, task_id: int, tool_call_id: str, request: DecisionRequest) -> None:
        """用户对一次等待决定的工具调用作出决定：校验与入队归 ``ToolDecisionService``，不等工具执行。

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
        """读取 Task 的 canonical snapshot。

        先校验 Task 存在：空 Task 与不存在的 Task 在投影层都表现为空消息列表，只能在这里区分。

        异常:
            KeyError: Task 不存在。
        """
        self._tasks.get_task(task_id)
        return self._states.get_state(task_id)


_DEFERRED_CANCELLED_NOTICE = (
    "The user cancelled the background work started by tool call {tool_call_id}; its result "
    "will not arrive. Do not wait for it or start it again unless the user asks."
)
