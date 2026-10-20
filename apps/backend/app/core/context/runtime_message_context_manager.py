"""Task context 的唯一运行时协调入口；SDK 负责模型聚合，本层负责持久化与协议闭合。"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Literal

from openai.lib.streaming.chat import ParsedChatCompletionSnapshot
from openai.types.chat import (
    ChatCompletionMessageParam,
)

from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.core.context import SystemPromptBuilder
from app.core.context.context_entry import ContextEntry
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
from app.service.conversation_run.conversation_task_context_service import (
    TASK_SYSTEM_PROMPT_SEQUENCE,
    ConversationTaskContextService,
)


@dataclass
class RuntimeMessageContextManager:
    """提供一个 task 的 Agent context 读写边界。"""

    current_task_id: int
    agent_profile: AgentProfile
    context_service: ConversationTaskContextService
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
        """恢复 Task 固定系统提示词并加载其余 context working copy。

        新 Task 使用当前配置构建并持久化完整 system prompt；已有 Task 始终恢复首次持久化的
        内容，因此配置变化不会改写历史 Task 的模型消息前缀。
        """
        service = self.context_service
        self._system_entry = service.get_system_prompt(self.current_task_id)
        if self._system_entry is None:
            system_prompt_text = SystemPromptBuilder.build(
                self.agent_profile, self.workspace.root_path
            )
            self._system_entry = service.create_system_prompt(
                self.current_task_id,
                {"role": "system", "content": system_prompt_text},
            )
        entries = service.entries_in_context(self.current_task_id)
        self._entries = [
            entry for entry in entries if entry.sequence != TASK_SYSTEM_PROMPT_SEQUENCE
        ]
        self._message_sequence = self.context_service.max_sequence(self.current_task_id) + 1

    def begin_run(
        self,
        run: ConversationRunRecord,
    ) -> None:
        """绑定当前 Run；历史上下文已在构造时加载。编辑用例负责事务截断并卸载旧 working copy。"""

        if run.task_id != self.current_task_id:
            raise ValueError(f"run {run.id} belongs to task {run.task_id}")

        self.current_run_id = run.id
        # 草稿行属于上一个 Run 的调用栈，绑定新 Run 时不得沿用到本次调用。
        self._draft_response = None

    def add_message(
        self,
        message: ChatCompletionMessageParam,
        *,
        include_in_context: bool = True,
        metadata: MessageMetadata | None = None,
        project_to_transport: bool = True,
        run_id: int | None = None,
    ) -> Literal["appended", "replaced"]:
        """写入完整 SDK 消息；工具结果必须属于当前最后一批调用的同一 Run。

        既有结果（含等待/中断占位）原地覆盖，保留消息身份和序号；缺少调用依据的新结果
        在写库前报 ContextProtocolError，不伪造调用或丢弃执行结果。流式预览的取消占位
        可以没有纳入上下文的调用，但必须 include_in_context=False，仅供 Transport 重建。
        替换时提交完整 metadata，旧等待请求和错误展示不会被隐式保留。
        """

        target_run_id = self.current_run_id if run_id is None else run_id
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
            self.context_service.replace_message(
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
            message, target_run_id, include_in_context, resolved_metadata, project_to_transport
        )
        return "appended"

    def _append_message(
        self,
        message: ChatCompletionMessageParam,
        run_id: int | None,
        include_in_context: bool,
        metadata: MessageMetadata,
        project_to_transport: bool = True,
    ) -> ContextEntry:
        """调用方已验证结果归属；落库成功后才推进 working copy 和序号。"""
        entry = self.context_service.append(
            self.current_task_id,
            run_id,
            message,
            self._message_sequence,
            include_in_context,
            metadata,
            project_to_transport=project_to_transport,
        )
        self._message_sequence += 1
        if include_in_context:
            self._entries.append(entry)
        return entry

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
        self.context_service.replace_message(
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
            self._entries.append(draft)
            self._draft_response = None
        return draft

    def abandon_model_response(self) -> None:
        """结束未准入的响应草稿；保留数据库展示行，下一次模型请求使用新消息身份。"""
        self._draft_response = None

    @property
    def next_sequence(self) -> int:
        """供 inbox 转移事务分配连续序号；调用方必须持有 Task 操作闸门。"""
        return self._message_sequence

    def accept_committed_inputs(self, entries: list[ContextEntry]) -> None:
        """接纳已提交的 inbox 转移结果，不重复落库或发送 Transport 事件。"""
        self._entries.extend(entries)
        self._message_sequence += len(entries)

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
        """闭合工具协议后返回**模型请求副本**：附件定位符已解析为 data URL。

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
