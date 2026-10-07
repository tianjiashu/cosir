"""Task 级 LangChain context 的运行时访问器。

本模块只维护模型调用所需的内存工作副本；持久化事实由
``ConversationTaskContextService`` 负责，``ContextEntry.run_id`` 始终随消息保存。

``add_message`` 是**工具结果**（取消/崩溃补的占位与真实结果）的唯一写入入口：命中同一条调用的
既有结果行时按 ``plan_tool_call_closure`` 的配对规则原地覆盖，追加则是默认行为。assistant 草稿走
另一条链路：``add_message_chunk`` 把流式 ``AIMessageChunk`` 演进为不纳入上下文的持久化草稿，
``flush_message_chunk`` 按 ``running`` / ``cancel`` / ``finalize`` 三态写同一行，其中 ``finalize``
一次完成收口（可在收口时交付修订版消息）——canonical 侧一条 assistant 消息只占一行
（``is_streaming`` 草稿行另计，取消或崩溃遗留的草稿不进入模型上下文）。
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
    ToolMessage,
)

from app.assistant_transport.event import UserInputAppendedEvent, build_user_input_parts
from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.core.context import SystemPromptBuilder
from app.core.context.context_compressor.context_compressor import ContextCompressor
from app.core.context.context_entry import ContextEntry
from app.core.context.streaming_message_state import StreamingMessageState
from app.core.context.tool_call_closure import (
    build_placeholder_tool_message,
    plan_tool_call_closure,
)
from app.core.runtime.execution_mode import ExecutionMode
from app.models import (
    ConversationRunFileAttachment,
    ConversationRunRecord,
)
from app.models.conversation_task_context import (
    ConversationTaskContextRecord,
    TransportMetadata,
)
from app.service.depends import (
    get_conversation_event_projector,
    get_conversation_task_context_service,
)
from app.service.task.conversation_task_context_service import (
    TASK_SYSTEM_PROMPT_SEQUENCE,
    ConversationTaskContextService,
)
from app.utils.message_content import content_to_text



@dataclass
class RuntimeContextManager:
    """提供一个 task 的 LangChain context 读写边界。"""

    current_task_id: int
    agent_profile: AgentProfile
    workspace_root: str = ""
    compressor: ContextCompressor | None = None
    current_run_id: int | None = None
    # fork Task 的运行时标记；它只描述 Task 身份，不改变 context 持久化规则。
    is_fork: bool = False
    # 纯内存用例注入的 context service 实现；生产路径恒为 None，回落到全局装配的实例。
    # 显式声明而非运行期探测属性：它是契约端口，缺失/未装配必须走明确分支而不是 getattr 静默降级。
    context_service: ConversationTaskContextService | None = field(default=None, init=False)
    # 固定持久化的 Task system prompt，不参与压缩；每次模型调用均位于消息前缀。
    _system_entry: ContextEntry | None = field(default=None, init=False)
    _entries: list[ContextEntry] = field(default_factory=list, init=False)
    # 序号由 RuntimeContextManager 独自分配，context service 只负责持久化：构造时
    # （__post_init__）从持久化 context 恢复下一个可用序号；此后每条新消息（含流式草稿首 chunk）
    # 落库时占用一个序号并自增；flush 的 finalize 态原地复用草稿已占用的序号，不推进游标。
    _message_sequence: int = field(default=0, init=False)
    # key=(run_id, stream_id) → 一条流式草稿的进程内聚合状态。复合 key 区分不同 run / step
    # 的草稿；partial 不进 _entries，避免被下一次模型调用误读。
    _streaming_messages: dict[tuple[int | None, str], StreamingMessageState] = field(
        default_factory=dict, init=False
    )

    def fork_context_manager(self, task_id: int) -> RuntimeContextManager:
        """为已复制 context 的目标 Task 创建独立的 fork manager。

        参数:
            task_id: 已完成持久化复制的目标 Task 标识。

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
        """恢复 Task 固定系统提示词并加载其余 context working copy。

        新 Task 使用当前配置构建并持久化完整 system prompt；已有 Task 始终恢复首次持久化的
        内容，因此配置变化不会改写历史 Task 的模型消息前缀。
        """
        service = self._require_context_service()
        self._system_entry = service.get_system_prompt(self.current_task_id)
        if self._system_entry is None:
            system_prompt_text = SystemPromptBuilder.build(self.agent_profile, self.workspace_root)
            self._system_entry = service.create_system_prompt(
                self.current_task_id,
                SystemMessage(content=system_prompt_text),
            )
        entries = service.entries_in_context(self.current_task_id)
        self._entries = [
            entry for entry in entries if entry.sequence != TASK_SYSTEM_PROMPT_SEQUENCE
        ]
        self._message_sequence = (
                self._require_context_service().max_sequence(self.current_task_id) + 1
        )

    def _require_context_service(self) -> ConversationTaskContextService:
        """返回 context service 端口：注入实现优先，否则回落到全局装配的实例。

        注入走类体显式声明的 ``context_service`` 字段（默认 ``None``）；生产路径不设置它，
        因此始终回落到全局 provider——provider 未装配时立即失败，不做静默降级。
        """
        if self.context_service is not None:
            return self.context_service
        return get_conversation_task_context_service()

    def begin_run(
        self,
        run: ConversationRunRecord,
        execution_mode: ExecutionMode = "fresh",
    ) -> None:
        """绑定 run，并从 context 中分离历史与当前 run 条目。

        参数:
            run: 待执行的 Conversation Run。
            execution_mode: ``fresh`` 清理该 run 的旧消息；``resume`` 保留并重新加载
                该 run 已持久化的消息。
        返回:
            无。

        异常:
            ValueError: run 不属于当前 task。

        副作用:
            更新 run 归属并准备当前 Task context。
        """

        if run.task_id != self.current_task_id:
            raise ValueError(f"run {run.id} belongs to task {run.task_id}")

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
        # SQLite 读取最后序号并推进一次，后续序号只由本类分配——add_message 与流式草稿首 chunk
        # 各占用一个序号，flush 的 finalize 态复用草稿序号而不推进。否则首轮
        # 使用 0/1 后，第二轮会再次尝试写入 1，触发 (task_id, sequence) 唯一约束。
        self.current_run_id = run.id

    def add_message(
            self,
            message: BaseMessage,
            *,
            include_in_context: bool = True,
            transport_metadata: TransportMetadata | None = None,
            run_id: int | None = None,
    ) -> Literal["appended", "replaced"]:
        """把一条完整 LangChain 消息写入 canonical context（工具结果可覆盖既有行）。

        追加是默认行为；唯一例外是 ``ToolMessage`` 命中了**同一条工具调用的既有结果行**——判据与
        补占位共用 ``plan_tool_call_closure``：只有落在最后一条携带 ``tool_calls`` 的 ``AIMessage``
        **之后**、且 ``tool_call_id`` 与归属 Run 都相同的那一行才算命中。命中即**原地覆盖**（沿用
        该行的 ``sequence`` 与 ``run_id``），让取消/崩溃遗留的 ``cancelled`` 占位被真实结果顶掉，
        且不并排留下第二行（模型协议上一条调用只有一个结果）。

        为什么必须按配对规则定位：``tool_call_id`` 由模型给出，可跨 Run、甚至跨同一 Run 的不同
        model 步复用；按 id 全表匹配会覆盖掉别的调用已有的结果行，并使当前调用永久不闭合。

        判据为何取内存副本：``_entries`` 装载自 ``include_in_context=True`` 的全部行，重启后由
        context service 重建；运行期的行删除也都经本类（``begin_run`` 移除该 Run 的条目，编辑重跑
        由命令层在无活跃 Run 时先删行再由本类重建），因此等价于持久化事实，无需数据库侧的唯一列。

        参数:
            message: 完整 LangChain 消息；不接受流式 chunk。
            include_in_context: 是否加入当前模型输入；默认 True。覆盖分支恒视为纳入上下文。
            transport_metadata: 该消息对应的 Transport 元数据；无则为 None。覆盖时 ``None`` 表示
                **保留既有行的 metadata**（见 ``ConversationTaskContextCrud.replace_message``），
                因此覆盖真实结果必须给出终态，否则会保留占位携带的 ``cancelled``。
            run_id: 该消息归属的 Conversation Run；为 None 时归属当前 Run。协议占位需要写回
                **产生该工具调用的 Run**，避免把历史调用的闭合结果记到新 Run 上；匹配既有行时也要求
                归属相同。

        返回:
            ``appended``：新增一行；``replaced``：覆盖了既有工具结果行。

        异常:
            KeyError: 覆盖时命中的行在替换前被删除（调用顺序错误）。
            持久化失败（含序号冲突）向上传播。

        副作用:
            追加时新增一行 canonical context 并占用一个序号，``include_in_context=True`` 时追加进
            内存模型上下文；覆盖时原地更新该行（不推进序号游标）、同步内存条目的消息，并记 info
            ``tool_result_row_replaced``。
        """

        target_run_id = self.current_run_id if run_id is None else run_id
        # 类型窄化不进入推导式作用域，故先固化标识；后续日志与覆盖分支都复用它。
        tool_call_id = message.tool_call_id if isinstance(message, ToolMessage) else ""
        existing: ContextEntry | None = None
        if tool_call_id:
            # 若 tool_call_id 非空，就用 plan_tool_call_closure(self._entries) 查找与该调用 ID
            # 配对的工具结果。
            plan = plan_tool_call_closure(self._entries)
            paired = (
                next((slot for slot in plan.slots if slot.call_id == tool_call_id), None)
                if plan is not None
                else None
            )
            if paired is not None and paired.tool_index is not None:
                candidate = self._entries[paired.tool_index]
                if candidate.run_id == target_run_id:
                    existing = candidate
        if existing is not None:
            # 工具结果幂等：常见于系统此前为未完成调用补过一条 cancelled 占位，续跑得到真实结果
            # 后就把它原地更新为真实 ToolMessage——用 replace_message(...) 保留原来的 sequence
            # 和 run_id、更新消息内容与 Transport 元数据，库里始终只有一条结果，不会同一调用
            # 留两行。
            self._require_context_service().replace_message(
                ConversationTaskContextRecord(
                    task_id=self.current_task_id,
                    run_id=existing.run_id,
                    message=message,
                    include_in_context=True,
                    sequence=existing.sequence,
                    transport_metadata=transport_metadata,
                    is_streaming=False,
                )
            )
            existing.message = message
            log.info(
                "tool_result_row_replaced",
                extra={
                    "msg": "同一工具调用的既有结果行已被真实结果覆盖",
                    "data": {
                        "task_id": self.current_task_id,
                        "run_id": existing.run_id,
                        "tool_call_id": tool_call_id,
                        "sequence": existing.sequence,
                    },
                },
            )
            return "replaced"
        sequence = self._message_sequence
        self._require_context_service().append(
            self.current_task_id,
            target_run_id,
            message,
            sequence,
            include_in_context,
            transport_metadata,
        )
        self._message_sequence += 1
        if not include_in_context:
            return "appended"
        self._entries.append(ContextEntry(message, target_run_id, sequence))
        return "appended"

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
            首个 chunk 新增一条 partial context 行，后续 chunk 更新该行，草稿不进入模型上下文；
            实时 Transport 增量仍由 workflow stream 负责。
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
            mode: Literal["running", "cancel", "finalize"] = "running",
            message: AIMessage | None = None,
    ) -> AIMessage | None:
        """把一条流式草稿按 ``mode`` 写入它已占用的那一行（同一行、同一序号，不新增行）。

        三种语义共用同一个前提——**同一条 assistant 消息全流程只占一个 ``sequence``**：草稿行在
        ``add_message_chunk`` 首个 chunk 时创建，本方法总是**原地替换**该行，既不新增行，也不推进
        消息序号游标。区别在写入形态与内存草稿的处置：

        - ``running``（默认）：写 partial（``is_streaming=True`` / ``include_in_context=False``）并
          **保留**内存草稿供后续 chunk 继续累积；``add_message_chunk`` 的节流刷写即走此态。
        - ``cancel``：run 已终止不再累积，写 partial 后**丢弃**内存草稿。
        - ``finalize``：收口——把草稿升格为 canonical（``is_streaming=False`` /
          ``include_in_context=True``）、丢弃内存草稿，并把该消息追加进内存模型上下文。
          ``message`` 用于交付**修订版**：``model_node`` 借此把 ``tool_calls`` 按工具生命周期记录
          重写、清空 ``invalid_tool_calls``（LangChain 解析失败的非法调用不得进入 provider 请求）；
          不传则固化内存聚合结果。草稿行在收口前既非 canonical 也不进模型上下文，因此修订时机不
          影响正确性。

        参数:
            stream_id: 本次流式会话的唯一标识，通常为 ``step-N``。
            run_id: 该草稿归属 Run；缺省使用当前 Run。写入不重新推导归属——草稿 key 里的 Run 才是
                事实，否则两次取数之间 ``current_run_id`` 变化会把该行改到别的 Run。
            mode: ``running`` / ``cancel`` / ``finalize``。
            message: 仅 ``finalize`` 可用：修订后的完整 ``AIMessage``；为 ``None`` 时使用内存聚合
                结果。其余模式传入即报错，避免入参被静默忽略。

        返回:
            写入该行的 ``AIMessage``；``running`` / ``cancel`` 且无对应草稿时返回 ``None``。

        异常:
            ValueError: ``mode`` 不是三态之一，或非 ``finalize`` 模式传入了 ``message``。
            KeyError: ``finalize`` 时该 ``(run_id, stream_id)`` 没有草稿（同一 ``stream_id`` 被重复
                收口，或草稿已被 ``cancel`` 态消费）。
            sqlalchemy.exc.SQLAlchemyError: 持久化失败——``finalize`` 时内存草稿已被消费且该消息
                未进入 canonical，同一 ``stream_id`` 无法重试（run 会走失败收敛）。

        副作用:
            ``running`` / ``cancel`` 只更新 partial 行（前者额外更新刷写进度并保留内存草稿）；
            ``finalize`` 原地更新该行为 canonical、丢弃内存草稿并在 ``_entries`` 末尾追加该消息。
            三种语义都不推进消息序号游标。不触发 Transport 增量——实时投影由 workflow custom
            stream 负责。
        """

        if mode not in ("running", "cancel", "finalize"):
            raise ValueError(f"unknown flush mode {mode}")
        finalizing = mode == "finalize"
        if message is not None and not finalizing:
            raise ValueError("message is only accepted in finalize mode")
        target_run_id = self.current_run_id if run_id is None else run_id
        key = (target_run_id, stream_id)
        # cancel / finalize 都消费草稿并移除内存 state；中途 flush 保留 state 以便继续累积。
        state = (
            self._streaming_messages.pop(key, None)
            if finalizing or mode == "cancel"
            else self._streaming_messages.get(key)
        )
        if state is None:
            if finalizing:
                raise KeyError(f"no streaming draft for run {target_run_id} / {stream_id}")
            return None
        written = _as_ai_message(state.chunk) if message is None else message
        self._require_context_service().replace_message(
            ConversationTaskContextRecord(
                task_id=self.current_task_id,
                # 按草稿自身归属回写：草稿行创建时用的是 target_run_id，缺省传 None 不得把归属清空。
                run_id=target_run_id,
                message=written,
                include_in_context=finalizing,
                sequence=state.sequence,
                is_streaming=not finalizing,
            )
        )
        if finalizing:
            self._entries.append(ContextEntry(written, target_run_id, state.sequence))
            return written
        if mode == "running":
            # 只有 running 会继续累积，才需要推进刷写进度；cancel 已丢弃该草稿，写它是无效写。
            state.persisted_text_length = len(content_to_text(state.chunk.content))
            state.last_persisted_at = time.monotonic()
        return written

    def _streaming_needs_flush(self, state: StreamingMessageState) -> bool:
        """根据字符和时间阈值决定是否刷写流式草稿。"""

        text_length = len(content_to_text(state.chunk.content))
        return (
                text_length - state.persisted_text_length >= Constant.Context.STREAMING_PERSIST_MIN_CHARS
                or time.monotonic() - state.last_persisted_at
                >= Constant.Context.STREAMING_PERSIST_MAX_INTERVAL_SECONDS
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
            经 ``add_message`` 落库一条 ``HumanMessage``，并向 Transport 投影一条
            ``UserInputAppendedEvent``；已存在时只记一条 info 日志后返回；文本和图片均为空时写一条
            warning 日志（历史空输入 Run 不应因此中断执行）。图片 ref 只记 workspace 相对路径，
            不包含二进制。
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
        content: str | list[dict[str, str]]
        if paths:
            content_blocks: list[dict[str, str]] = []
            if text and text.strip():
                # 只发图片时不得产出空 text 块：部分厂商（Anthropic）拒绝空文本块，而 langchain
                # 的翻译层只对字符串 content 丢弃空串，不会替列表里的空块兜底。
                content_blocks.append({"type": "text", "text": text})
            content_blocks.extend({"type": "image", "file_id": path} for path in paths)
            content = content_blocks
        else:
            # 纯文本输入保持字符串 content：provider 兼容性最好，也是历史形状。
            content = text
        self.add_message(HumanMessage(content=cast(Any, content)))
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
        return True

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
            RuntimeError: 缺失槽位在追加占位时命中了既有结果行（同一条 ``AIMessage`` 出现重复
                ``call_id`` 一类畸形数据）——显式失败，避免复用旧行后索引失配。
            sqlalchemy.exc.SQLAlchemyError: 持久化失败，由 ``add_message`` 向上传播。

        副作用:
            为每个未配对调用调用 ``add_message``：写回 ``_entries``、经
            ``context_service.append`` 落库（``include_in_context=True``、随行写入调用所属
            ``run_id`` 与 ``TransportMetadata(status="cancelled")``）；已有但错位的 ToolMessage
            只在当前 working copy 中重排，不改写上下文事实。占位落库后下次加载即命中配对，天然
            幂等（占位追加在目标消息之后，下次取数即被配对规则认到）。
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
            # missing 槽（``tool_index is None``）按配对规则不可能命中既有行——命中就有 tool_index。
            # 因此这里必须是「追加」；真的命中说明出现了同 id 重复槽一类畸形数据，显式失败比静默
            # 复用旧行、并让下面的索引失配更好。
            if self.add_message(
                build_placeholder_tool_message(slot.call_id, slot.tool_name),
                include_in_context=True,
                # 占位必须随行写入 Transport 终态：快照重建按 Run 分组配对时直接读
                # ``transport_metadata["status"]``，缺失 metadata 会让重建在取 status 时崩溃，
                # 或产出不在 wire 白名单内的 None 状态。取值与 build_pair_tool_part 对未配对
                # 调用的默认终态保持一致。
                transport_metadata=TransportMetadata(status="cancelled"),
                # 归属产生该调用的 Run，而不是恰好正在执行的新 Run。
                run_id=plan.target_run_id,
            ) != "appended":
                raise RuntimeError(
                    f"tool call placeholder must be appended, but the result row of "
                    f"{slot.call_id} already exists"
                )
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

        取数前先闭合最后一条工具调用消息上未配对的结果（崩溃/取消遗留）：为缺失结果补
        ``cancelled`` 占位，并把已落库但错位的结果搬回该消息之后。判据与覆盖策略共用
        ``plan_tool_call_closure``，因此**只在配对规则可达范围内闭合**——``AIMessage`` 之前残留的
        「反向孤儿」结果行不在其中（属 ``tool_call_closure`` 已登记的缺陷），此时模型输入仍不闭合。

        返回:
            模型输入副本：固定 system 提示词 + 当前 Task 全部纳入上下文的条目。返回深拷贝，调用方
            改写不影响 working copy。

        异常:
            RuntimeError: 收口占位时命中畸形数据（见 :meth:`_close_unclosed_tool_calls`），或
                system entry 未初始化。
            sqlalchemy.exc.SQLAlchemyError: 补占位落库失败。

        副作用:
            检测到未配对调用时经 ``_close_unclosed_tool_calls`` 补占位（写库并更新 working copy）；
            无论是否有未配对调用，都把 working copy 中的 assistant 消息重建为只保留
            ``id`` / ``content`` / ``tool_calls`` / ``additional_kwargs`` 的形态——
            ``invalid_tool_calls`` 等字段不得进入 provider 请求。
        """

        self._close_unclosed_tool_calls()
        for entry in self._effective_entries():
            if entry.message.type == "ai":
                source_ai_message = cast(AIMessage, entry.message)
                entry.message = AIMessage(
                    id=source_ai_message.id,
                    content=source_ai_message.content,
                    tool_calls=source_ai_message.tool_calls,
                    additional_kwargs=source_ai_message.additional_kwargs,
                )
        return [entry.message for entry in copy.deepcopy(self._effective_entries())]

    def _effective_entries(self) -> list[ContextEntry]:
        """返回 system、历史和当前 run 条目的有序列表。"""

        if self._system_entry is None:
            raise RuntimeError("system entry is not initialized")
        return [self._system_entry, *self._entries]


def _as_ai_message(chunk: AIMessageChunk) -> AIMessage:
    """把聚合 chunk 转成可序列化的标准 ``AIMessage``。"""

    serialized = chunk.model_dump()
    serialized["type"] = "ai"
    return AIMessage.model_validate(serialized)
