"""Task 级 LangChain context 的运行时访问器。

本模块只维护模型调用所需的内存工作副本；持久化事实由
``ConversationTaskContextService`` 负责，``ContextEntry.run_id`` 始终随消息保存。
流式 ``AIMessageChunk`` 通过 ``add_message_chunk`` 写入不纳入模型上下文的持久化草稿；
只有收口后的完整消息才会进入模型 context。
"""

from __future__ import annotations

import copy
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)

from app.assistant_transport.event import UserInputAppendedEvent, build_user_input_parts
from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.context import SystemPromptBuilder
from app.core.context.context_compressor.context_compressor import ContextCompressor
from app.core.context.context_entry import ContextEntry
from app.core.context.context_listener.context_listener import ContextListener
from app.core.context.context_listener.listener_event import ContextEventType, ListenerEvent
from app.core.context.context_listener.listener_result import ListenerResult
from app.core.context.streaming_message_state import StreamingMessageState
from app.core.context.tool_call_closure import (
    build_placeholder_tool_message,
    plan_tool_call_closure,
)
from app.core.runtime.execution_mode import ExecutionMode
from app.models import (
    ConversationRunFileAttachment,
    ConversationRunRecord,
    TaskRecord,
    WorkspaceRecord,
)
from app.models.conversation_task_context import (
    ConversationTaskContextRecord,
    TransportMetadata,
)
from app.service.depends import (
    get_conversation_event_projector,
    get_conversation_task_context_service,
)
from app.service.provider.capability_service import CapabilityService
from app.service.task.conversation_task_context_service import ConversationTaskContextService
from app.utils.message_content import content_to_text

STREAMING_PERSIST_MIN_CHARS = 64
STREAMING_PERSIST_MAX_INTERVAL_SECONDS = 0.25


@dataclass
class RuntimeContextManager:
    """提供一个 task 的 LangChain context 读写和 listener 边界。"""

    current_task_id: int
    agent_profile: AgentProfile
    workspace_root: str = ""
    total_tokens: int = 0
    used_tokens: int = 0
    compressor: ContextCompressor | None = None
    current_run_id: int | None = None
    # fork Task 的运行时标记；它只描述 Task 身份，不改变 context 持久化规则。
    is_fork: bool = False
    # task 级 system prompt 条目，不参与压缩。
    _system_entry: ContextEntry | None = field(default=None, init=False)
    _entries: list[ContextEntry] = field(default_factory=list, init=False)
    # begin_run 从持久化 context 恢复下一个可用序号，add_message 落库后自增。
    # 序号游标由 RuntimeContextManager 独自管理；context service 只负责持久化。
    _message_sequence: int = field(default=0, init=False)
    # 上下文变化订阅者列表：按 order 排序，按需插入。
    _listeners: list[ContextListener] = field(default_factory=list, init=False)
    # 当前 Conversation Run 实际暴露给模型的工具 schema；只保存运行时配置，不落库。
    _tool_schemas: tuple[Mapping[str, Any], ...] = field(default_factory=tuple, init=False)
    # key=(run_id, stream_id) → 一条流式草稿的进程内聚合状态。复合 key 区分不同 run / step
    # 的草稿；partial 不进 _entries，避免被下一次模型调用误读。
    _streaming_messages: dict[tuple[int | None, str], StreamingMessageState] = field(
        default_factory=dict, init=False
    )

    @staticmethod
    def ensure_get_runtime_context_manager(
            agent_profile: AgentProfile,
            current_workspace: WorkspaceRecord,
            current_task: TaskRecord,
    ) -> RuntimeContextManager:
        """获取或创建 task 级 context 管理器。

        参数:
            agent_profile: 当前 Agent 档案。
            current_workspace: 当前工作区记录。
            current_task: 当前任务记录。

            纯内存测试可通过在实例上挂载 ``context_service`` 属性注入 mock，由
            ``_require_context_service`` 优先采用，从而绕过全局 service 装配。

            ** agent启动时已经有task锁，无需再加锁。**

        返回:
            与 task 绑定的 context 管理器。

        异常:
            无。

        副作用:
            首次创建时加载 Task context，并注册占用统计 listener。
        """

        from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

        return task_runtime_spaces.get_or_create(current_task.id).get_context_manager(
            agent_profile=agent_profile,
            current_workspace=current_workspace,
            current_task=current_task,
        )

    def fork_context_manager(self, task_id: int) -> RuntimeContextManager:
        """为已复制 context 的目标 Task 创建独立的 fork manager。

        参数:
            task_id: 已完成持久化复制的目标 Task 标识。
            run_id: 源 Task 的历史边界，仅用于调用语义记录；不绑定目标当前 run。

        返回:
            绑定目标 Task、初始没有当前 run 且带有 ``is_fork`` 标记的新 manager。

        异常:
            RuntimeError: context service 未装配。

        副作用:
            从目标 Task 的持久化 context 加载独立 working copy；不修改源 manager。
        """

        return RuntimeContextManager(
            current_task_id=task_id,
            agent_profile=copy.deepcopy(self.agent_profile),
            workspace_root=self.workspace_root,
            is_fork=True,
        )

    def __post_init__(self) -> None:
        """确保并加载 Task 级 system prompt，再建立内存 working copy。"""

        service = self._require_context_service()
        prompt = SystemMessage(
            content=SystemPromptBuilder.build(self.agent_profile, self.workspace_root)
        )
        entries = service.entries_in_context(self.current_task_id)
        self._system_entry = ContextEntry(prompt, None, -1)
        self._entries = list(entries)
        self._message_sequence = (
                self._require_context_service().max_sequence(self.current_task_id) + 1
        )

    def _require_context_service(self) -> ConversationTaskContextService:
        """返回已装配的 context service；未装配时立即失败。

        若实例持有注入的 ``context_service``（纯内存测试场景）则优先返回，否则回落到
        全局 ``get_conversation_task_context_service()``。生产路径不设置该属性，故始终走全局。
        """
        injected = getattr(self, "context_service", None)
        if injected is not None:
            return injected
        return get_conversation_task_context_service()

    def begin_run(
            self,
            run: ConversationRunRecord,
            execution_mode: ExecutionMode = "fresh",
            tool_schemas: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """绑定 run，并从 context 中分离历史与当前 run 条目。

        参数:
            run: 待执行的 Conversation Run。
            execution_mode: ``fresh`` 清理该 run 的旧消息；``resume`` 保留并重新加载
                该 run 已持久化的消息。
            tool_schemas: 当前 Run 实际绑定给模型的模型侧工具 schema；只保存在运行时，
                不写入 Task context 持久化记录。

        返回:
            无。

        异常:
            ValueError: run 不属于当前 task。

        副作用:
            更新 run 归属、模型窗口并通知 listener 当前 Task context。
        """

        if run.task_id != self.current_task_id:
            raise ValueError(f"run {run.id} belongs to task {run.task_id}")

        # manager 跨 Run 复用，必须在绑定新 Run 时替换而不是沿用旧工具集合。
        self._tool_schemas = tuple(copy.deepcopy(schema) for schema in tool_schemas)

        if execution_mode == "fresh":
            # fresh 仍按持久化 run 身份清理，而不是依赖进程内指针，避免重跑时重复
            # 追加 user/tool message。正常首次执行没有同 run 条目，因此是幂等空操作。
            service = self._require_context_service()
            service.delete_by_run_id(self.current_task_id, run.id)
            self._entries = [entry for entry in self._entries if entry.run_id != run.id]
            self._streaming_messages = {
                key: value
                for key, value in self._streaming_messages.items()
                if key[0] != run.id
            }
        # ``max_sequence`` 返回的是最后一个已使用的序号，而不是下一个可用序号。
        # RuntimeContextManager 是 Task context 序号的唯一运行时 owner：恢复时从
        # SQLite 读取最后序号并推进一次，后续消息只由 ``add_message`` 自增。否则首轮
        # 使用 0/1 后，第二轮会再次尝试写入 1，触发 (task_id, sequence) 唯一约束。
        self.current_run_id = run.id
        self.total_tokens = CapabilityService.get_model_context_window(run.model_name or "")

    def add_change_listener(self, listener: ContextListener) -> RuntimeContextManager:
        """注册一个按 order 执行的 context listener。

        同一个 manager 会跨 Conversation Run 复用，因此同一 listener 类型的后续注册
        会替换旧实例，避免 context 变更被重复处理，也避免旧 run 的 event emitter
        在新 run 的 graph stream 外继续收到回调。
        """

        if listener.main_agent_only and self.agent_profile.agent_type is not AgentProfileType.MAIN:
            return self
        for index, current in enumerate(self._listeners):
            if type(current) is type(listener):
                self._listeners[index] = listener
                self._listeners.sort(key=lambda item: item.order)
                return self
        self._listeners.append(listener)
        self._listeners.sort(key=lambda item: item.order)
        return self

    def add_message(
            self,
            message: BaseMessage,
            *,
            include_in_context: bool = True,
            transport_metadata: TransportMetadata | None = None,
            run_id: int | None = None,
    ) -> bool:
        """追加一条完整 LangChain 消息到 Task context。

        参数:
            message: 完整 LangChain 消息；不接受流式 chunk。
            include_in_context: 是否加入当前模型输入；默认 True。
            transport_metadata: 该消息对应的 Transport 元数据；无则为 None。
            run_id: 该消息归属的 Conversation Run；为 None 时归属当前 Run。协议占位需要
                写回**产生该工具调用的 Run**，避免把历史调用的闭合结果记到新 Run 上。

        返回:
            ``True`` 表示 canonical context 新增；``False`` 表示同一工具结果已存在。

        异常:
            ValueError: 传入 ``AIMessageChunk``。

        副作用:
            通过 context owner 持久化完整消息，并更新当前内存副本。
        """
        target_run_id = self.current_run_id if run_id is None else run_id
        sequence = self._message_sequence
        created = self._require_context_service().append(
            self.current_task_id,
            target_run_id,
            message,
            sequence,
            include_in_context,
            transport_metadata,
        )
        if created is False:
            return False
        self._message_sequence += 1
        if not include_in_context:
            return True
        self._entries.append(ContextEntry(message, target_run_id, sequence))
        self.mark_context_changed(ContextEventType.ADD_MESSAGE, self._effective_entries())
        return True

    def add_message_chunk(
            self,
            chunk: AIMessageChunk,
            *,
            stream_id: str,
            run_id: int | None = None,
    ) -> AIMessage:
        """累计并持久化一条模型流式 assistant 草稿。

        同一 ``(run_id, stream_id)`` 只占用一个 context sequence；后续 chunk 原子替换该
        行，而不是追加新消息。草稿标记为 ``is_streaming=True`` 且不纳入模型上下文，因而
        可被 Transport 冷重建，但不会在取消/崩溃后的 resume 中误作为完整 assistant message
        发送给 provider。

        参数:
            chunk: 模型 ``astream`` 产出的 ``AIMessageChunk``。
            stream_id: 当前模型步骤的稳定标识，通常为 ``step-N``。
            run_id: 消息归属 Run；缺省使用当前 Run。

        返回:
            截至当前 chunk 的聚合 ``AIMessage``。

        异常:
            ValueError: chunk 类型不正确或 stream_id 为空；持久化错误向上传播。

        副作用:
            首个 chunk 新增一条 partial context 行，后续 chunk 更新该行；不会触发 context
            usage listener 或 ADD_MESSAGE 事件，实时 Transport 增量仍由 workflow stream 负责。
        """

        if not isinstance(chunk, AIMessageChunk):
            raise ValueError("add_message_chunk requires AIMessageChunk")
        if not stream_id or not stream_id.strip():
            raise ValueError("stream_id must not be empty")
        target_run_id = self.current_run_id if run_id is None else run_id
        key = (target_run_id, stream_id)
        state = self._streaming_messages.get(key)
        if state is None:
            merged_chunk = chunk
            sequence = self._message_sequence
            self._require_context_service().append(
                self.current_task_id,
                target_run_id,
                _as_ai_message(merged_chunk),
                sequence,
                False,
                None,
                True,
            )
            self._message_sequence += 1
            state = StreamingMessageState(
                chunk=merged_chunk,
                sequence=sequence,
                persisted_text_length=len(content_to_text(merged_chunk.content)),
                last_persisted_at=time.monotonic(),
            )
        else:
            merged_chunk = state.chunk + chunk

        state.chunk = merged_chunk
        if self._streaming_needs_flush(state):
            self.flush_message_chunk(stream_id=stream_id, run_id=target_run_id)
        self._streaming_messages[key] = state
        return _as_ai_message(merged_chunk)

    def flush_message_chunk(
            self,
            *,
            stream_id: str,
            run_id: int | None = None,
            mode: Literal["complete", "cancel", "running"] = "running",
    ) -> AIMessage | None:
        """把当前流式草稿刷入数据库；按 ``mode`` 分三种语义态。

        - ``running``（默认）：以 partial 状态（``is_streaming=True``）落库，**保留**内存
          state 供后续 chunk 继续累积。add_message_chunk 的节流刷写即走此态。
        - ``cancel``：同样 partial 落盘，但 run 已终止不再累积，故**丢弃**内存 state；不加入
          模型上下文、不触发 listener。
        - ``complete``：收口为普通 canonical assistant 消息，复用原 ``sequence`` 更新同一行，
          并仅在收口时加入模型上下文与触发 ``ADD_MESSAGE`` listener。

        参数:
            stream_id: 本次流式会话的唯一标识，通常为 ``step-N``。
            run_id: 消息归属 Run；缺省使用当前 Run。
            mode: ``running`` / ``cancel`` / ``complete``。

        返回:
            本次刷写得到的 ``AIMessage``；无对应草稿时返回 ``None``。

        异常:
            持久化错误向上传播。

        副作用:
            running / cancel 更新 partial 行但不触发 listener；complete 触发 ``ADD_MESSAGE``
            并把消息加入内存 entries；实时 Transport 增量由 workflow stream 负责。
        """

        target_run_id = self.current_run_id if run_id is None else run_id
        key = (target_run_id, stream_id)
        # 收口 flush 与取消 flush 都消费草稿并移除内存 state；中途 flush 保留 state 以便继续累积。
        state = (
            self._streaming_messages.pop(key, None)
            if mode in {"complete", "cancel"}
            else self._streaming_messages.get(key)
        )
        if state is None:
            return None
        if mode == "complete":
            finalized = _as_ai_message(state.chunk)
            self._require_context_service().replace_streaming_message(
                ConversationTaskContextRecord(
                    task_id=self.current_task_id,
                    run_id=target_run_id,
                    message=finalized,
                    include_in_context=True,
                    sequence=state.sequence,
                    is_streaming=False,
                )
            )
            self._entries.append(ContextEntry(finalized, target_run_id, state.sequence))
            self.mark_context_changed(ContextEventType.ADD_MESSAGE, self._effective_entries())
            return _as_ai_message(state.chunk)
        # 中途 flush：保持 partial 状态，不触发 listener。
        self._require_context_service().replace_streaming_message(
            ConversationTaskContextRecord(
                task_id=self.current_task_id,
                run_id=run_id,
                message=_as_ai_message(state.chunk),
                include_in_context=False,
                sequence=state.sequence,
                is_streaming=True,
            )
        )
        state.persisted_text_length = len(content_to_text(state.chunk.content))
        state.last_persisted_at = time.monotonic()
        return _as_ai_message(state.chunk)

    def _streaming_needs_flush(self, state: StreamingMessageState) -> bool:
        """根据字符和时间阈值决定是否刷写流式草稿。"""

        text_length = len(content_to_text(state.chunk.content))
        return (
                text_length - state.persisted_text_length >= STREAMING_PERSIST_MIN_CHARS
                or time.monotonic() - state.last_persisted_at
                >= STREAMING_PERSIST_MAX_INTERVAL_SECONDS
        )

    def ensure_run_user_message(
            self,
            text: str,
            image_paths: Sequence[str] | None = None,
            display_text: str | None = None,
            file_attachments: Sequence[ConversationRunFileAttachment] | None = None,
    ) -> bool:
        """确保当前 Run 在上下文中恰好有一条初始 user 消息。

        该消息是 Run 的 canonical 输入事实（``ConversationRunRecord.input_text``）；调用方
        必须在启动 graph 之前调用，使首个 ``load_message`` 能把用户输入交给模型。

        幂等判据取 **canonical context 事实**（该 Run 是否已有 ``HumanMessage`` 行），而不是
        ``execution_mode``——三种启动场景对「是否已有」的期望恰好由这一步区分：

        - **新 Run**（``fresh``）：``begin_run`` 未清到任何旧条目（首次执行）⇒ 无 ⇒ 写入；
        - **编辑重跑**（``fresh``）：``begin_run`` 已按 run 清空该 Run 的条目 ⇒ 无 ⇒ 写入；
        - **续跑**（``resume``）：``begin_run`` 保留该 Run 的既有条目 ⇒ 有 ⇒ 跳过；
        - 续跑但上次崩在写入之前（``resume`` 且查无此消息）⇒ 补写，否则 graph 拿不到输入。

        进程内 ``self._entries`` **不可**作判据：它只覆盖当前进程写入过的消息，后端重启后
        为空，会把「已有」误判成「没有」而重复写入（历史缺陷：续跑曾因此写入第二条 user
        消息，使快照重建出现重复 ``user-{run_id}``，并让整个 Task 的历史接口 500）。

        参数:
            text: 该 Run 的输入文本。
            image_paths: 已最终化的 workspace-relative 图片路径；只作为自定义 image ref
                写入 canonical context，模型调用前由 model-input boundary 解析成 provider block。
            display_text: 可选的 Transport 用户展示文本。普通文件 token 保留在此文本中，
                不把本机路径泄漏到历史消息 UI；canonical context 仍使用 ``text`` 供模型读取。
            file_attachments: 已持久化的普通文件元数据；只用于构造 Transport file parts，
                不会把其中的本机路径发送到 Transport。
        返回:
            ``True`` 表示本次写入了一条 ``HumanMessage``；``False`` 表示该 Run 已存在，或
            文本和图片均为空被跳过。

        异常:
            无；持久化失败由 ``add_message`` 向上传播。

        副作用:
            经 ``add_message`` 落库一条 ``HumanMessage`` 并触发上下文变更监听；已存在时只记
            一条 info 日志后返回；文本和图片均为空时写一条 warning 日志（历史空输入 Run 不应
            因此中断执行）。图片 ref 不包含二进制。
        """

        paths = tuple(path.strip() for path in (image_paths or ()) if path and path.strip())
        if not (text and text.strip()) and not paths:
            log.warning(
                "run_user_message_blank",
                extra={
                    "msg": "Run 输入文本与图片均为空，跳过初始 user 消息写入",
                    "data": {"task_id": self.current_task_id, "run_id": self.current_run_id},
                },
            )
            return False
        if self._current_run_has_user_message():
            log.info(
                "run_user_message_exists",
                extra={
                    "msg": "当前 Run 已有初始 user 消息，跳过写入",
                    "data": {"task_id": self.current_task_id, "run_id": self.current_run_id},
                },
            )
            return False
        if paths:
            content_blocks: list[dict[str, str]] = []
            if text and text.strip():
                content_blocks.append({"type": "text", "text": text})
            content_blocks.extend({"type": "cosir_image_ref", "path": path} for path in paths)
            content: str | list[dict[str, str]] = content_blocks
        else:
            content = text
        message = self.add_message(HumanMessage(content=cast(Any, content)))
        get_conversation_event_projector().process(
            UserInputAppendedEvent(
                task_id=self.current_task_id,
                run_id=self.current_run_id,
                parts=build_user_input_parts(
                    display_text if display_text is not None else text,
                    paths,
                    cast(Sequence[Mapping[str, str]], file_attachments or ()),
                ),
            )
        )
        return message

    def _current_run_has_user_message(self) -> bool:
        """返回当前 Run 是否已在 canonical context 中拥有初始 user 消息。

        只读 ``conversation_task_contexts``（唯一持久化真相），**不**使用进程内
        ``self._entries``：后者只覆盖当前进程写入过的消息，后端重启后为空，会把「已有」
        误判成「没有」而重复写入。
        """

        entries = self._require_context_service().entries_in_context(self.current_task_id)
        return any(
            entry.run_id == self.current_run_id and isinstance(entry.message, HumanMessage)
            for entry in entries
        )

    def _close_unclosed_tool_calls(self) -> None:
        """闭合最后一条工具调用消息上未配对的结果，并把结果归位到该消息之后。

        未闭合指某个 ``AIMessage`` 携带 ``tool_calls``，但上下文中不存在 ``tool_call_id``
        与之匹配的 ``ToolMessage``（模型已请求工具但结果未落库，通常由 run 崩溃或取消
        导致）。链路里这**只可能出现在最后一条携带 tool_calls 的 ``AIMessage`` 上**：

        - 该消息必然是它所属 Run 的最后一条消息：``model_node`` 落库后只会再有同批
          ``ToolMessage``、本 Run 的延迟修复 ``SystemMessage``（写在占位之后），或下一
          Run 的 ``HumanMessage``；
        - 每个 model 步入场都会取数一次，未闭合不跨 model 步，因此历史里不会留下更早
          的未闭合调用。

        因此本方法只从尾部定位这一条消息，不再扫描/认领更早的 AI 消息：更早的消息都不
        可能带着未配对调用留存至今（每步入场都会取数收口，真出现也早已被 provider 以协议
        错误暴露），不存在需要修复的「历史未闭合」状态。

        一个 assistant 消息的多个 tool call 必须按 ``tool_calls`` 顺序紧随其后。结果即使
        已经落库，也可能被后续消息（下一 Run 的 ``HumanMessage``、延迟修复 ``SystemMessage``）
        隔开，或因为占位的全局序号更大而排在它们之后；此时把匹配结果搬回该消息之后，并
        保留其他消息的相对顺序。

        占位与其闭合的调用归属**同一个 Run**（取该 ``AIMessage`` 的 ``run_id``）：快照重建
        按 Run 分组配对，若把占位记到当前 Run，重建会在新 Run 分组里看到一条没有对应 AI
        调用的 ``ToolMessage`` 而失败。

        不论原因是取消还是崩溃，占位的业务语义统一标记为 ``cancelled``（模型通道正文与
        Transport ``status`` 同口径）；canonical 工具执行事实的终态由取消分支经
        ``cancel_tool_calls`` 单独写入，本方法只负责模型协议层面的配对闭合，不触碰领域事实。

        返回:
            无。

        异常:
            无；持久化异常由 ``add_message`` 向上传播。

        副作用:
            为每个未配对调用调用 ``add_message``：写回 ``_entries``、经
            ``context_service.append`` 落库（``include_in_context=True``、随行写入调用所属
            ``run_id`` 与 ``TransportMetadata(status="cancelled")``）并触发上下文变更监听；
            已有但错位的 ToolMessage 只在当前 working copy 中重排，不改写上下文事实。占位
            落库后下次加载即命中配对，天然幂等。
        """
        entries = self._entries
        plan = plan_tool_call_closure(entries)
        if plan is None:
            return

        # 占位会追加到 ``entries`` 末尾，因此后续按索引遍历必须固定原长度。
        original_length = len(entries)
        normalized: list[ContextEntry] = list(entries[: plan.target_index + 1])
        created_placeholder_count = 0
        reordered_tool_count = 0
        appended_count = 0

        for slot in plan.slots:
            if slot.tool_index is not None:
                normalized.append(entries[slot.tool_index])
                if slot.tool_index != plan.target_index + 1 + appended_count:
                    reordered_tool_count += 1
                appended_count += 1
                continue

            previous_length = len(self._entries)
            created = self.add_message(
                build_placeholder_tool_message(slot.call_id, slot.tool_name),
                include_in_context=True,
                # 占位必须随行写入 Transport 终态：快照重建按 Run 分组配对时直接读
                # ``transport_metadata["status"]``，缺失 metadata 会让重建在取 status 时崩溃，
                # 或产出不在 wire 白名单内的 None 状态。取值与 build_pair_tool_part 对未配对
                # 调用的默认终态保持一致。
                transport_metadata=TransportMetadata(status="cancelled"),
                # 归属产生该调用的 Run，而不是恰好正在执行的新 Run。
                run_id=plan.target_run_id,
            )
            if created is False:
                # 同 (task, run, tool_call_id) 已落库但不在当前 working copy（例如未纳入
                # 上下文的历史行）：不能重复写入，也不凭空构造条目，记 warning 后跳过。
                log.warning(
                    "runtime_context_tool_call_placeholder_skipped",
                    extra={
                        "msg": "该工具调用已有同名结果行，跳过占位写入",
                        "data": {
                            "task_id": self.current_task_id,
                            "run_id": plan.target_run_id,
                            "tool_call_id": slot.call_id,
                        },
                    },
                )
                continue
            normalized.append(self._entries[previous_length])
            created_placeholder_count += 1
            appended_count += 1

        claimed_tool_indices = plan.claimed_tool_indices
        for index in range(plan.target_index + 1, original_length):
            if index in claimed_tool_indices:
                continue
            normalized.append(entries[index])

        if not created_placeholder_count and not reordered_tool_count:
            return

        self._entries = normalized
        log.warning(
            "runtime_context_tool_call_protocol_repaired",
            extra={
                "msg": "上下文中的工具调用结果已闭合或恢复到合法消息顺序",
                "data": {
                    "task_id": self.current_task_id,
                    "created_placeholder_count": created_placeholder_count,
                    "reordered_tool_count": reordered_tool_count,
                },
            },
        )

    def load_message(self) -> list[BaseMessage]:
        """返回 system prompt 加 Task context 的模型输入副本。

        取数前先闭合最后一条工具调用消息上未配对的结果（崩溃/取消遗留），保证返回给模型的
        上下文协议闭合。

        副作用:
            若检测到未配对调用，经 ``_close_unclosed_tool_calls`` -> ``add_message`` 补占位会
            写回上下文并触发变更通知；无未配对调用时不修改上下文。
        """
        self._close_unclosed_tool_calls()
        message_list = []
        for entry in self._effective_entries():
            if entry.message.type == "ai":
                source_ai_message = cast(AIMessage, entry.message)
                entry.message = AIMessage(
                    id=source_ai_message.id,
                    content=source_ai_message.content,
                    tool_calls=source_ai_message.tool_calls,
                    additional_kwargs=source_ai_message.additional_kwargs,

                )
            message_list.append(entry)
        return [entry.message for entry in copy.deepcopy(self._effective_entries())]

    def _effective_entries(self) -> list[ContextEntry]:
        """返回 system、历史和当前 run 条目的有序列表。"""

        if self._system_entry is None:
            raise RuntimeError("system entry is not initialized")
        return [self._system_entry, *self._entries]

    def mark_context_changed(
            self,
            event_type: ContextEventType,
            entries: list[ContextEntry],
    ) -> None:
        """向 listener 发布 context 完整快照。"""

        result = ListenerResult(self.used_tokens)
        snapshot = copy.deepcopy(entries)
        for listener in self._listeners:
            try:
                listener.listen(
                    ListenerEvent(
                        event_type,
                        snapshot,
                        self.used_tokens,
                        self.total_tokens,
                        self._tool_schemas,
                    ),
                    result,
                )
            except Exception:
                log.exception(
                    "context_listener_failed",
                    extra={
                        "msg": "context 已落库，旁路 listener 失败并被降级",
                        "data": {
                            "task_id": self.current_task_id,
                            "run_id": self.current_run_id,
                            "listener": type(listener).__name__,
                            "event_type": event_type.value,
                        },
                    },
                )
        self.used_tokens = result.usage


def _as_ai_message(chunk: AIMessageChunk) -> AIMessage:
    """把聚合 chunk 转成可序列化的标准 ``AIMessage``。"""

    serialized = chunk.model_dump()
    serialized["type"] = "ai"
    return AIMessage.model_validate(serialized)
