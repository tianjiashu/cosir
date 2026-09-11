"""Task 级 LangChain context 的运行时访问器。

本模块只维护模型调用所需的内存工作副本；持久化事实由
``ConversationTaskContextService`` 负责，``ContextEntry.run_id`` 始终随消息保存。
流式 ``AIMessageChunk`` 不进入本模块，只有完整消息才会追加到 context。
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage

from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.context import SystemPromptBuilder
from app.core.context.context_compressor.context_compressor import ContextCompressor
from app.core.context.context_entry import ContextEntry
from app.core.context.context_listener.context_listener import ContextListener
from app.core.context.context_listener.listener_event import ContextEventType, ListenerEvent
from app.core.context.context_listener.listener_result import ListenerResult
from app.core.runtime.execution_mode import ExecutionMode
from app.models import ConversationRunRecord, TaskRecord, WorkspaceRecord
from app.models.json_helpers import TransportPart, TransportToolResult
from app.service.provider.capability_service import CapabilityService
from app.service.task.conversation_task_context_service import ConversationTaskContextService


@dataclass
class RuntimeContextManager:
    """提供一个 task 的 LangChain context 读写和 listener 边界。"""

    current_task_id: int
    agent_profile: AgentProfile
    workspace_root: str = ""
    total_tokens: int = 0
    used_tokens: int = 0
    compressor: ContextCompressor | None = None
    context_service: ConversationTaskContextService | None = None
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
            context_service: Task context 唯一持久化 owner；纯内存测试可传 None。
            run: 用于设置模型窗口的当前 Conversation Run。、

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
            context_service=self._require_context_service(),
            is_fork=True,
        )

    def __post_init__(self) -> None:
        """确保并加载 Task 级 system prompt，再建立内存 working copy。"""

        service = self._require_context_service()
        prompt = SystemMessage(
            content=SystemPromptBuilder.build(self.agent_profile, self.workspace_root)
        )
        service.ensure_system_message(self.current_task_id, prompt)
        entries = service.entries_in_context(self.current_task_id)
        persisted_system = next(
            (
                entry
                for entry in entries
                if entry.run_id is None and isinstance(entry.message, SystemMessage)
            ),
            None,
        )
        self._system_entry = persisted_system or ContextEntry(prompt, None, -1)
        self._entries = [entry for entry in entries if entry is not persisted_system]
        self.mark_context_changed(ContextEventType.LOAD_HISTORY, self._effective_entries())

    def _require_context_service(self) -> ConversationTaskContextService:
        """返回已装配的 context service；未装配时立即失败。"""

        if self.context_service is None:
            raise RuntimeError("context_service is required for persisted context operations")
        return self.context_service

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
            reset_fresh = getattr(service, "reset_run_for_fresh", None)
            if callable(reset_fresh):
                reset_fresh(self.current_task_id, run.id)
            else:
                service.delete_by_run_id(self.current_task_id, run.id)
            self._entries = [entry for entry in self._entries if entry.run_id != run.id]
            if callable(reset_fresh):
                self._entries = service.entries_in_context(self.current_task_id)
        else:
            # resume 可能发生在后端重启后，必须从 SQLite 重新装载 working copy；同进程
            # 恢复也通过同一条路径，确保 ContextEntry.run_id/sequence 与持久化一致。
            loaded_entries = self._require_context_service().entries_in_context(
                self.current_task_id
            )
            self._entries = [
                entry
                for entry in loaded_entries
                if not (entry.run_id is None and isinstance(entry.message, SystemMessage))
            ]
        # ``max_sequence`` 返回的是最后一个已使用的序号，而不是下一个可用序号。
        # RuntimeContextManager 是 Task context 序号的唯一运行时 owner：恢复时从
        # SQLite 读取最后序号并推进一次，后续消息只由 ``add_message`` 自增。否则首轮
        # 使用 0/1 后，第二轮会再次尝试写入 1，触发 (task_id, sequence) 唯一约束。
        self._message_sequence = (
            self._require_context_service().max_sequence(self.current_task_id) + 1
        )
        self.current_run_id = run.id
        self.total_tokens = CapabilityService.get_model_context_window(run.model_name or "")
        if execution_mode == "resume":
            # resume 不会走 fresh 的 add_message，但 UI 仍需要立即得到已有
            # context 的 used/window；重新投影完整 working copy，避免重启后只看到 0/null。
            self.mark_context_changed(ContextEventType.LOAD_HISTORY, self._effective_entries())

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
        transport_parts: Sequence[TransportPart] | None = None,
        tool_result: TransportToolResult | None = None,
    ) -> bool:
        """追加一条完整 LangChain 消息到 Task context。

        参数:
            message: 完整 LangChain 消息；不接受流式 chunk。
            include_in_context: 是否加入当前模型输入；默认 True。
            allow_write_event_failure: listener 旁路失败时是否继续。

        返回:
            ``True`` 表示 canonical context 新增；``False`` 表示同一工具结果已存在。

        异常:
            ValueError: 传入 ``AIMessageChunk``。

        副作用:
            通过 context owner 持久化完整消息，并更新当前内存副本。
        """

        if (
            include_in_context
            and isinstance(message, ToolMessage)
            and message.tool_call_id
            and any(
                entry.run_id == self.current_run_id
                and isinstance(entry.message, ToolMessage)
                and entry.message.tool_call_id == message.tool_call_id
                for entry in self._entries
            )
        ):
            log.info(
                "runtime_context_tool_message_duplicate_ignored",
                extra={
                    "msg": "重复恢复的 ToolMessage 已幂等忽略",
                    "data": {
                        "task_id": self.current_task_id,
                        "tool_call_id": message.tool_call_id,
                    },
                },
            )
            return False

        if include_in_context and isinstance(message, SystemMessage):
            message_kind = message.additional_kwargs.get("cosir_message_kind")
            if message_kind == "tool_call_repair" and any(
                entry.run_id == self.current_run_id
                and isinstance(entry.message, SystemMessage)
                and entry.message.additional_kwargs.get("cosir_message_kind") == message_kind
                and entry.message.content == message.content
                for entry in self._entries
            ):
                log.info(
                    "runtime_context_repair_message_duplicate_ignored",
                    extra={
                        "msg": "重复恢复的工具调用修复提示已幂等忽略",
                        "data": {
                            "task_id": self.current_task_id,
                            "run_id": self.current_run_id,
                        },
                    },
                )
                return False

        sequence = self._message_sequence
        append_kwargs: dict[str, object] = {}
        if transport_parts is not None:
            append_kwargs["transport_parts"] = list(transport_parts)
        if tool_result is not None:
            append_kwargs["tool_result"] = tool_result
        created = self._require_context_service().append(
            self.current_task_id,
            self.current_run_id,
            message,
            sequence,
            include_in_context,
            **append_kwargs,
        )
        if created is False:
            return False
        self._message_sequence += 1
        if not include_in_context:
            return True
        self._entries.append(ContextEntry(message, self.current_run_id, sequence))
        self.mark_context_changed(ContextEventType.ADD_MESSAGE, self._effective_entries())
        return True

    def _close_unclosed_tool_calls(self) -> None:
        """闭合并规范化上下文中未配对或错位的工具调用结果。

        未闭合指某个 ``AIMessage`` 携带 ``tool_calls``，但后续上下文中不存在
        ``tool_call_id`` 与之匹配的 ``ToolMessage``。通常由 run 崩溃或被取消导致
        （模型已请求工具但结果未落库）。此处作为**唯一收口点**，为每个未闭合调用补
        一条 ``ToolMessage`` 占位，写回内存与数据库，使模型协议始终闭合、可继续。

        历史上下文还可能已经存在匹配的 ``ToolMessage``，但它被追加在后续
        ``HumanMessage`` / ``SystemMessage`` 之后。仅按 ``tool_call_id`` 判断存在会把这种
        序列误认为合法；本方法会把匹配结果移动到对应 ``AIMessage`` 后面，并保留其他
        消息的相对顺序。

        不论原因是取消还是崩溃，占位的业务语义统一标记为 ``cancelled``；canonical
        工具执行事实的终态由取消分支经 ``cancel_tool_calls`` 单独写入，本方法只负责
        模型协议层面的配对闭合，不触碰领域事实。

        返回:
            无。

        副作用:
            为每个未闭合调用调用 ``add_message``：写回 ``_entries``、经
            ``context_service.append`` 落库（``include_in_context=True``）并触发
            上下文变更监听；已有但错位的 ToolMessage 只在当前 working copy 中重排，
            不改写上下文事实。占位落库后下次加载即命中配对，天然幂等。
        """
        entries = list(self._entries)
        tool_entries_by_call_id: dict[str, list[tuple[int, ContextEntry]]] = {}
        for index, entry in enumerate(entries):
            message = entry.message
            if isinstance(message, ToolMessage) and message.tool_call_id:
                tool_entries_by_call_id.setdefault(message.tool_call_id, []).append((index, entry))

        normalized: list[ContextEntry] = []
        claimed_tool_entry_indices: set[int] = set()
        created_placeholder_count = 0
        reordered_tool_count = 0

        for index, entry in enumerate(entries):
            if index in claimed_tool_entry_indices:
                continue

            message = entry.message
            normalized.append(entry)
            if not isinstance(message, AIMessage) or not message.tool_calls:
                continue

            # 一个 assistant 消息的多个 tool call 必须按 tool_calls 顺序紧随其后。
            # 结果即使已经落库，也可能因为取消后追加新用户消息而出现在更后面；只要
            # 它位于该 assistant 消息之后且尚未被其他调用认领，就把它移动到这里。
            result_offset = 0
            for call in message.tool_calls:
                call_id = call.get("id")
                if not call_id:
                    continue
                candidates = [
                    (tool_index, tool_entry)
                    for tool_index, tool_entry in tool_entries_by_call_id.get(call_id, [])
                    if tool_index > index and tool_index not in claimed_tool_entry_indices
                ]
                if candidates:
                    tool_index, tool_entry = candidates[0]
                    claimed_tool_entry_indices.add(tool_index)
                    normalized.append(tool_entry)
                    if tool_index != index + 1 + result_offset:
                        reordered_tool_count += 1
                    result_offset += 1
                    continue

                placeholder = ToolMessage(
                    content=(
                        f"The tool call '{call.get('name') or 'unknown'}' (id={call_id}) "
                        f"did not produce a result because the run was cancelled or "
                        f"interrupted; no tool output is available."
                    ),
                    tool_call_id=call_id,
                )
                previous_length = len(self._entries)
                self.add_message(placeholder, include_in_context=True)
                normalized.append(self._entries[previous_length])
                created_placeholder_count += 1

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

    def _normalize_tool_call_message_order(self) -> None:
        """把历史遗留的修复 ``SystemMessage`` 移到关联工具结果之后。

        新代码通过 ``deferred_repair_message`` 保证消息顺序，但旧版本可能已经持久化了
        ``AIMessage(tool_calls) -> SystemMessage -> ToolMessage``。该顺序会在下一次请求时
        再次触发 provider 的 400，因此每次加载模型上下文前做一次内存侧兼容修复。这里只
        调整模型输入副本的顺序，不删除或重写 canonical context 事实；后续追加消息仍使用
        原有序号，下一次加载会再次得到同样的规范顺序。

        参数:
            无。

        返回:
            无。

        异常:
            无。无法识别的消息保持原顺序，不阻断上下文加载。

        副作用:
            可能调整 ``_entries`` 的内存顺序并写一条结构化诊断日志；不直接写数据库。
        """
        normalized: list[ContextEntry] = []
        deferred_systems: list[ContextEntry] = []
        pending_call_ids: set[str] = set()
        moved_count = 0

        for entry in self._entries:
            message = entry.message
            if isinstance(message, SystemMessage) and pending_call_ids:
                deferred_systems.append(entry)
                moved_count += 1
                continue

            normalized.append(entry)
            if isinstance(message, AIMessage):
                pending_call_ids.update(
                    str(call.get("id")) for call in message.tool_calls if call.get("id")
                )
            elif isinstance(message, ToolMessage) and message.tool_call_id:
                pending_call_ids.discard(message.tool_call_id)

            if not pending_call_ids and deferred_systems:
                normalized.extend(deferred_systems)
                deferred_systems = []

        # 悬空调用没有结果时，仍把被延迟的 system 消息放到当前可见历史末尾；调用方已在
        # 本方法前执行 ``_close_unclosed_tool_calls``，因此此处不会再把占位插到它后面。
        normalized.extend(deferred_systems)
        if moved_count:
            self._entries = normalized
            log.warning(
                "runtime_context_tool_message_order_repaired",
                extra={
                    "msg": "加载上下文时修复历史 SystemMessage 与 ToolMessage 的顺序",
                    "data": {
                        "task_id": self.current_task_id,
                        "moved_system_message_count": moved_count,
                    },
                },
            )

    def load_message(self) -> list[BaseMessage]:
        """返回 system prompt 加 Task context 的模型输入副本。

        取数前先统一闭合未配对的工具调用占位（崩溃/取消遗留），保证返回给模型的
        上下文协议闭合。

        副作用:
            若检测到悬空调用，经 ``_close_unclosed_tool_calls`` -> ``add_message`` 补占位会
            写回上下文并触发变更通知；无悬空时不修改上下文。
        """
        # 先补齐悬空 tool call，再移动修复 SystemMessage；否则历史形如
        # AI(tool_calls) -> SystemMessage（无 ToolMessage）会在排序后重新被占位插到 System
        # 后面，仍然触发 provider 400。
        self._close_unclosed_tool_calls()
        self._normalize_tool_call_message_order()
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
