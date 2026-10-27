"""Task context 的唯一运行时协调入口；SDK 负责模型聚合，本层负责 working copy、序号与协议闭合。

行级持久化（追加、原地替换、固定系统提示词）经 ``ContextStore``，本层不直接碰存储。
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Literal

from openai.lib.streaming.chat import ParsedChatCompletionSnapshot
from openai.types.chat import (
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
)
from sqlalchemy.orm import Session

from app.config.logging.logger import log
from app.core.agents.agent_runtime_config import AgentRuntimeConfig
from app.core.context import SystemPromptBuilder
from app.core.context.context_entry import ContextEntry
from app.core.context.context_store import (
    TASK_SYSTEM_PROMPT_SEQUENCE,
    ContextStore,
)
from app.core.context.tool_call_closure import (
    ContextProtocolError,
    ToolCallClosurePlan,
    build_placeholder_tool_message,
    plan_tool_call_closures,
)
from app.core.context.vision_input import resolve_messages_for_model
from app.core.llm_provider.message_codec import assistant_message
from app.models import (
    ConversationRunRecord,
    WorkspaceRecord,
)
from app.models.conversation_task_context import (
    ConversationTaskContextRecord,
)
from app.models.message_metadata import (
    AssistantMessageMetadata,
    MessageMetadata,
    ToolMessageMetadata,
    empty_message_metadata,
)


@dataclass
class RuntimeMessageContextManager:
    """一个 Task 的 Agent context 读写边界与内存消息列表 owner。

    内存消息列表（``_entries``）是模型请求的唯一来源，``load_message`` 只读它；每次
    :meth:`begin_run` 从数据库按 ``include_in_context`` 重建一次，编辑重跑、fork 等改库用例因此
    不必通知或卸载本对象。列表与序号只经 :meth:`_track` 推进，且只接纳已写库的行。
    """

    current_task_id: int
    # 只用其身份与执行资源（角色、提示词、工具名）构建 Task 首个系统提示词，及供配置广播过滤。
    agent: AgentRuntimeConfig
    # 行级持久化边界：追加、原地替换、序号与 Task 固定系统提示词都经它读写。
    context_store: ContextStore
    # 工作区记录：系统提示词取 ``root_path``，模型请求的附件解析取 ``id``。必填——缺工作区
    # 会生成一份没有工作区事实的系统提示词并被持久化成 Task 固定前缀，属于不可逆的静默降级。
    workspace: WorkspaceRecord
    current_run_id: int | None = None
    # 固定持久化的 Task system prompt，不参与压缩；每次模型调用均位于消息前缀。
    _system_entry: ContextEntry | None = field(default=None, init=False)
    _entries: list[ContextEntry] = field(default_factory=list, init=False)
    # 从持久化 context 恢复序号；每条新消息在成功落库后占用一个序号。
    _message_sequence: int = field(default=0, init=False)
    # 当前模型调用正在流式写入的响应行身份（``include_in_context`` 仍为 False）。
    # 只用于「同一次调用持续替换同一行」；提交或开始下一次调用即清空。
    _draft_response: ContextEntry | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        """恢复 Task 固定系统提示词；其余消息由 :meth:`begin_run` 加载。

        新 Task 使用当前配置构建并持久化完整 system prompt；已有 Task 始终恢复首次持久化的
        内容，因此配置变化不会改写历史 Task 的模型消息前缀。
        """
        store = self.context_store
        self._system_entry = store.get_system_prompt(self.current_task_id)
        if self._system_entry is None:
            system_prompt_text = SystemPromptBuilder.build(
                self.agent, self.workspace.root_path
            )
            self._system_entry = store.create_system_prompt(
                self.current_task_id,
                {"role": "system", "content": system_prompt_text},
            )

    def begin_run(self, run: ConversationRunRecord) -> None:
        """绑定当前 Run，并从数据库重建内存消息列表与序号。

        每次都重建而不是沿用上一个 Run 的内存列表：编辑重跑会把旧行移出上下文，fork、删除
        历史 Run 也会改库，无条件重建让这些用例无需卸载或刷新本对象。代价是每个 Run 多一次读库。
        """

        if run.task_id != self.current_task_id:
            raise ValueError(f"run {run.id} belongs to task {run.task_id}")

        self.current_run_id = run.id
        # 草稿行属于上一个 Run 的调用栈，绑定新 Run 时不得沿用到本次调用。
        self._draft_response = None
        entries = self.context_store.entries_in_context(self.current_task_id)
        self._entries = [
            entry for entry in entries if entry.sequence != TASK_SYSTEM_PROMPT_SEQUENCE
        ]
        self._message_sequence = self.context_store.max_sequence(self.current_task_id) + 1

    def _track(self, entry: ContextEntry, *, in_context: bool) -> None:
        """内存消息列表与序号的唯一推进入口；调用方保证该行已落库。

        序号按已落库行推进（流式草稿落库即占号，提交时不再推进）；只有进入模型上下文的行才加入
        内存列表。
        """
        self._message_sequence = max(self._message_sequence, entry.sequence + 1)
        if in_context:
            self._entries.append(entry)

    def add_message(
        self,
        message: ChatCompletionMessageParam,
        *,
        include_in_context: bool = True,
        metadata: MessageMetadata | None = None,
        project_to_transport: bool = True,
        message_id: int | None = None,
        session: Session | None = None,
    ) -> Literal["appended", "replaced"]:
        """写入完整 SDK 消息；工具结果必须属于当前最后一批调用的同一 Run。

        外部消息队列投递来的用户输入与通知也经本方法写入，不另设接纳入口。
        ``message_id`` 只有用户输入需要传：沿用队列行身份，排队与已消费阶段前端消息 id 不变；
        其余消息没有身份。``session`` 让追加加入调用方事务（``prepare_input`` 把队列确认与写入放在
        同一事务）；内存列表随即推进、不等提交——事务失败会经 Run 失败边界结束本次驱动，下个 Run
        的 :meth:`begin_run` 从库重建，不会留下错位的 working copy。

        既有结果（含等待/中断占位）原地覆盖，保留消息身份和序号；缺少调用依据的新结果
        在写库前报 ContextProtocolError，不伪造调用或丢弃执行结果。流式预览的取消占位
        可以没有纳入上下文的调用，但必须 include_in_context=False，仅供 Transport 重建。
        替换时提交完整 metadata，旧等待请求和错误展示不会被隐式保留。
        """

        target_run_id = self.current_run_id
        # 类型窄化不进入推导式作用域，故先固化标识；后续日志与覆盖分支都复用它。
        tool_call_id = message.get("tool_call_id") if message["role"] == "tool" else ""
        existing: ContextEntry | None = None
        if message["role"] == "tool":
            plans = self._plan_tool_calls()
            plan = plans[-1] if plans else None
            paired = (
                next((slot for slot in plan.slots if slot.call_id == tool_call_id), None)
                if plan is not None and plan.target_run_id == target_run_id
                else None
            )
            if paired is None and include_in_context:
                error = ContextProtocolError(
                    "tool_result_without_current_call",
                    ContextEntry(message, target_run_id, self._message_sequence),
                    message["tool_call_id"],
                )
                self._log_protocol_error(error)
                raise error
            if paired is not None and paired.tool_index is not None:
                existing = self._entries[paired.tool_index]
        resolved_metadata = (
            metadata if metadata is not None else empty_message_metadata(message["role"])
        )
        if existing is not None:
            # 同一调用可能先写等待或取消占位，随后由用户批准产生真实结果；
            # 用 replace_message(...) 保留原来的 sequence
            # 和 run_id、更新消息内容与 Transport 元数据，库里始终只有一条结果，不会同一调用
            # 留两行。
            self.context_store.replace_message(
                ConversationTaskContextRecord(
                    task_id=self.current_task_id,
                    run_id=existing.run_id,
                    message_id=existing.message_id,
                    message=message,
                    include_in_context=True,
                    sequence=existing.sequence,
                    project_to_transport=project_to_transport,
                    metadata=resolved_metadata,
                )
            )
            existing.message = message
            existing.metadata = resolved_metadata
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
        self._append_message(
            message,
            target_run_id,
            include_in_context,
            resolved_metadata,
            project_to_transport,
            message_id=message_id,
            session=session,
        )
        return "appended"

    def _append_message(
        self,
        message: ChatCompletionMessageParam,
        run_id: int | None,
        include_in_context: bool,
        metadata: MessageMetadata,
        project_to_transport: bool = True,
        *,
        message_id: int | None = None,
        session: Session | None = None,
    ) -> ContextEntry:
        """调用方已验证结果归属；写库成功后才推进内存列表和序号。"""
        entry = self.context_store.append(
            self.current_task_id,
            run_id,
            message,
            self._message_sequence,
            include_in_context,
            metadata,
            project_to_transport=project_to_transport,
            session=session,
            message_id=message_id,
        )
        self._track(entry, in_context=include_in_context)
        return entry

    def find_tool_call(self, tool_call_id: str) -> ChatCompletionMessageFunctionToolCallParam | None:
        """在纳入上下文的 assistant 消息里按身份找回一次工具调用（含名称与原始参数）。

        跨 Run 查找：HITL 调用可能在早先的 Run 里发起，决定在之后的 Run 才到。找不到（如已被
        编辑重跑移出上下文）时返回 ``None``，由调用方决定如何告知模型。
        """
        for entry in reversed(self._entries):
            if entry.message["role"] != "assistant":
                continue
            for call in entry.message.get("tool_calls") or ():
                if call["id"] == tool_call_id and call["type"] == "function":
                    return call
        return None

    def update_tool_result_metadata(
        self, tool_call_id: str, metadata: ToolMessageMetadata
    ) -> ToolMessageMetadata:
        """只替换一条工具结果行的展示 metadata，模型可见正文不变；返回实际写入的 metadata。

        HITL 执行结果另行追加为消息，等待占位仍是模型协议上的结果；这里只把占位的卡片状态推到
        终态，历史前缀因此不变。卡片是否可见（``hidden``）沿用原行，不随结果改变；结果没有带
        展示数据时沿用占位的（HITL 卡片由占位的展示数据决定，如 Agent Team 的方案预览）。

        异常:
            KeyError: 上下文中没有该调用的结果行。
        """
        entry = next(
            (
                item
                for item in reversed(self._entries)
                if item.message["role"] == "tool" and item.message["tool_call_id"] == tool_call_id
            ),
            None,
        )
        if entry is None:
            raise KeyError(tool_call_id)
        if isinstance(entry.metadata, ToolMessageMetadata):
            previous = entry.metadata
            metadata = metadata.model_copy(
                update={
                    "hidden": previous.hidden,
                    "display_data": metadata.display_data or previous.display_data,
                    "team_run_id": metadata.team_run_id or previous.team_run_id,
                }
            )
        self.context_store.replace_message(
            ConversationTaskContextRecord(
                task_id=self.current_task_id,
                run_id=entry.run_id,
                message_id=entry.message_id,
                message=entry.message,
                include_in_context=True,
                sequence=entry.sequence,
                project_to_transport=True,
                metadata=metadata,
            )
        )
        entry.metadata = metadata
        return metadata

    def save_model_response(
        self,
        snapshot: ParsedChatCompletionSnapshot,
        *,
        include_in_context: bool,
        registered_tool_names: Collection[str],
    ) -> ContextEntry:
        """直接保存 SDK 快照；同一次调用内持续替换同一行，不重新聚合 chunk。

        行身份由本层持有，调用方不再传递。流式写入（``include_in_context=False``）只保留身份
        供下一帧替换，不进入 working copy；响应结束并完成工具名过滤后，仅一次以
        ``include_in_context=True`` 提交，提交即清空草稿，下一次调用自然新起一行。中断行继续
        留库供展示，后续模型上下文不会加载它——它所在的模型调用要么已提交、要么终结了 Run，
        因此不会出现「下一次调用沿用中断行身份」的情况。
        """
        choice = snapshot.choices[0]
        message = assistant_message(choice.message, registered_tool_names)
        metadata = AssistantMessageMetadata(
            completion_id=snapshot.id,
            model=snapshot.model,
            finish_reason=choice.finish_reason,
            usage=snapshot.usage,
        )
        draft = self._draft_response
        if draft is None:
            entry = self._append_message(
                message,
                self.current_run_id,
                include_in_context,
                metadata,
            )
            # ``_append_message`` 已按准入决定写 working copy；只有未提交时需留身份供下一帧替换。
            if not include_in_context:
                self._draft_response = entry
            return entry
        self.context_store.replace_message(
            ConversationTaskContextRecord(
                task_id=self.current_task_id,
                run_id=draft.run_id,
                message_id=draft.message_id,
                message=message,
                include_in_context=include_in_context,
                sequence=draft.sequence,
                metadata=metadata,
            )
        )
        draft.message = message
        draft.metadata = metadata
        if include_in_context:
            # 提交进模型上下文即本次调用结束：清空草稿身份，下一次调用新起一行。
            self._track(draft, in_context=True)
            self._draft_response = None
        return draft

    def abandon_model_response(self) -> None:
        """结束未准入的响应草稿；保留数据库展示行，下一次模型请求使用新消息身份。"""
        self._draft_response = None

    def _log_protocol_error(self, error: ContextProtocolError) -> None:
        """记录可定位的上下文完整性错误，不泄露工具正文到 UI 错误通道。"""
        log.error(
            "runtime_context_tool_protocol_invalid",
            extra={
                "msg": "上下文工具结果缺少可靠配对依据，保留事实并停止执行",
                "data": {
                    "task_id": self.current_task_id,
                    "run_id": self.current_run_id,
                    **error.log_data(),
                },
            },
        )

    def _plan_tool_calls(self) -> tuple[ToolCallClosurePlan, ...]:
        try:
            return plan_tool_call_closures(self._entries)
        except ContextProtocolError as error:
            self._log_protocol_error(error)
            raise

    def close_unclosed_tool_calls(self) -> None:
        """按全部批次恢复模型协议顺序；只为真实调用补中断占位，不重放工具。

        先完成整段配对校验，避免遇到孤儿时已经写入部分占位。已落库结果紧跟所属调用，
        其余消息保持相对顺序；数据库序号不变，因此每次冷加载都按同一规则投影全部历史。
        占位沿用调用的 Run，持久化后可重建；协议错误保留事实并向 Run 失败边界传播。
        """
        plans = self._plan_tool_calls()
        if not plans:
            return
        entries = list(self._entries)
        by_index = {plan.target_index: plan for plan in plans}
        claimed = {index for plan in plans for index in plan.claimed_tool_indices}
        normalized: list[ContextEntry] = []
        created = 0
        reordered = 0
        for index, entry in enumerate(entries):
            if index in claimed:
                continue
            normalized.append(entry)
            plan = by_index.get(index)
            if plan is None:
                continue
            for offset, slot in enumerate(plan.slots, start=1):
                if slot.tool_index is not None:
                    normalized.append(entries[slot.tool_index])
                    reordered += int(slot.tool_index != index + offset)
                else:
                    normalized.append(
                        self._append_message(
                            build_placeholder_tool_message(slot.call_id, slot.tool_name),
                            plan.target_run_id,
                            True,
                            ToolMessageMetadata(status="cancelled"),
                        )
                    )
                    created += 1
        self._entries = normalized
        if created or reordered:
            log.warning(
                "runtime_context_tool_call_protocol_repaired",
                extra={
                    "msg": "上下文中的工具调用结果已闭合或恢复到合法消息顺序",
                    "data": {
                        "task_id": self.current_task_id,
                        "created_placeholder_count": created,
                        "reordered_tool_count": reordered,
                    },
                },
            )

    def load_message(self) -> list[ChatCompletionMessageParam]:
        """闭合工具协议后，由内存消息列表返回**模型请求副本**：附件定位符已解析为 data URL。

        canonical context 只保存 ``cosir-attachment://`` 定位符，解析结果（base64）只存在于返回
        副本，不会回写 working copy。返回副本由 ``resolve_messages_for_model`` 统一深拷贝。

        副作用:
            命中图片附件时读文件并 base64 编码，属于阻塞 IO；调用方必须放到工作线程执行
            （``asyncio.to_thread``），不得在事件循环里直接调用。
        """
        self.close_unclosed_tool_calls()
        return resolve_messages_for_model(
            [entry.message for entry in self._effective_entries()],
            workspace_id=self.workspace.id,
        )

    def _effective_entries(self) -> list[ContextEntry]:
        """返回 system、历史和当前 run 条目的有序列表。"""

        if self._system_entry is None:
            raise RuntimeError("system entry is not initialized")
        return [self._system_entry, *self._entries]
