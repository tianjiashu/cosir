"""Task 级 LangChain context 的运行时访问器。

本模块只维护模型调用所需的内存工作副本；持久化事实由
``ConversationTaskContextService`` 负责，``ContextEntry.run_id`` 始终随消息保存。
流式 ``AIMessageChunk`` 不进入本模块，只有完整消息才会追加到 context。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage

from app.core.agents.agent_profile import AgentProfile
from app.core.context import SystemPromptBuilder
from app.core.context.context_compressor.context_compressor import ContextCompressor
from app.core.context.context_entry import ContextEntry
from app.core.context.context_listener.context_listener import ContextListener
from app.core.context.context_listener.listener_event import ContextEventType, ListenerEvent
from app.core.context.context_listener.listener_result import ListenerResult
from app.core.runtime.execution_mode import ExecutionMode
from app.models import ConversationRunRecord, TaskRecord, WorkspaceRecord
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
    # 标记的在workflow期间，上下文是否有变化
    have_change: bool = field(default=False, init=False)

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

    def fork_context_manager(
        self, task_id: int
    ) -> RuntimeContextManager:
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
        """初始化不持久化的 system prompt 条目。"""

        self._system_entry = ContextEntry(
            SystemMessage(
                content=SystemPromptBuilder.build(self.agent_profile, self.workspace_root)
            ),
            None,
            -1,
        )
        self._entries = self._require_context_service().entries_in_context(self.current_task_id)
        self.mark_context_changed(ContextEventType.LOAD_HISTORY, self._effective_entries())

    def _require_context_service(self) -> ConversationTaskContextService:
        """返回已装配的 context service；未装配时立即失败。"""

        if self.context_service is None:
            raise RuntimeError("context_service is required for persisted context operations")
        return self.context_service

    def begin_run(
            self, run: ConversationRunRecord, execution_mode: ExecutionMode = "fresh"
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
            更新 run 归属、模型窗口并通知 listener 当前 Task context。
        """

        if run.task_id != self.current_task_id:
            raise ValueError(f"run {run.id} belongs to task {run.task_id}")

        if execution_mode == "fresh":
            # fresh 仍按持久化 run 身份清理，而不是依赖进程内指针，避免重跑时重复
            # 追加 user/tool message。正常首次执行没有同 run 条目，因此是幂等空操作。
            self._require_context_service().delete_by_run_id(self.current_task_id, run.id)
            self._entries = [entry for entry in self._entries if entry.run_id != run.id]
        else:
            # resume 可能发生在后端重启后，必须从 SQLite 重新装载 working copy；同进程
            # 恢复也通过同一条路径，确保 ContextEntry.run_id/sequence 与持久化一致。
            self._entries = self._require_context_service().entries_in_context(
                self.current_task_id
            )
        # ``max_sequence`` 返回的是最后一个已使用的序号，而不是下一个可用序号。
        # RuntimeContextManager 是 Task context 序号的唯一运行时 owner：恢复时从
        # SQLite 读取最后序号并推进一次，后续消息只由 ``add_message`` 自增。否则首轮
        # 使用 0/1 后，第二轮会再次尝试写入 1，触发 (task_id, sequence) 唯一约束。
        self._message_sequence = self._require_context_service().max_sequence(
            self.current_task_id
        ) + 1
        self.current_run_id = run.id
        self.total_tokens = CapabilityService.get_model_context_window(run.model_name or "")

    def add_change_listener(self, listener: ContextListener) -> RuntimeContextManager:
        """注册一个按 order 执行的 context listener。

        同一个 manager 会跨 Conversation Run 复用，因此同一 listener 类型的后续注册
        会替换旧实例，避免 context 变更被重复处理，也避免旧 run 的 event emitter
        在新 run 的 graph stream 外继续收到回调。
        """

        if listener.main_agent_only and not self.agent_profile.main_agent:
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
    ) -> None:
        """追加一条完整 LangChain 消息到 Task context。

        参数:
            message: 完整 LangChain 消息；不接受流式 chunk。
            include_in_context: 是否加入当前模型输入；默认 True。
            allow_write_event_failure: listener 旁路失败时是否继续。

        返回:
            无。

        异常:
            ValueError: 传入 ``AIMessageChunk``。

        副作用:
            通过 context owner 持久化完整消息，并更新当前内存副本。
        """

        if isinstance(message, AIMessage):
            # 仅保留 AIMessage 中的 content、 additional_kwargs、 tool_calls
            message = AIMessage(
                content=message.content,
                additional_kwargs=message.additional_kwargs,
                tool_calls=message.tool_calls,
            )

        sequence = self._message_sequence
        self._require_context_service().append(
            self.current_task_id,
            self.current_run_id,
            message,
            sequence,
            include_in_context,
        )
        self._message_sequence += 1
        if not include_in_context:
            return
        self._entries.append(ContextEntry(message, self.current_run_id, sequence))
        self.have_change = True
        self.mark_context_changed(ContextEventType.ADD_MESSAGE, self._effective_entries())

    def _close_unclosed_tool_calls(self) -> None:
        """检测并闭合上下文中未配对闭合的工具调用占位。

        未闭合指某个 ``AIMessage`` 携带 ``tool_calls``，但后续上下文中不存在
        ``tool_call_id`` 与之匹配的 ``ToolMessage``。通常由 run 崩溃或被取消导致
        （模型已请求工具但结果未落库）。此处作为**唯一收口点**，为每个未闭合调用补
        一条 ``ToolMessage`` 占位，写回内存与数据库，使模型协议始终闭合、可继续。

        不论原因是取消还是崩溃，占位的业务语义统一标记为 ``cancelled``；canonical
        工具执行事实的终态由取消分支经 ``cancel_tool_calls`` 单独写入，本方法只负责
        模型协议层面的配对闭合，不触碰领域事实。

        返回:
            无。

        副作用:
            为每个未闭合调用调用 ``add_message``：写回 ``_entries``、经
            ``context_service.append`` 落库（``include_in_context=True``）并触发
            上下文变更监听。占位落库后下次加载即命中配对，天然幂等。
        """
        pending_call_ids: dict[str, str | None] = {}
        for entry in self._effective_entries():
            message = entry.message
            if isinstance(message, AIMessage) and message.tool_calls:
                for call in message.tool_calls:
                    call_id = call.get("id")
                    if not call_id:
                        continue
                    name = call.get("name")
                    pending_call_ids.setdefault(call_id, name)
            elif isinstance(message, ToolMessage) and message.tool_call_id:
                pending_call_ids.pop(message.tool_call_id, None)
        if not pending_call_ids:
            return
        for call_id, tool_name in pending_call_ids.items():
            placeholder = ToolMessage(
                content=(
                    f"The tool call '{tool_name or 'unknown'}' (id={call_id}) "
                    f"did not produce a result because the run was cancelled or "
                    f"interrupted; no tool output is available."
                ),
                tool_call_id=call_id,
            )
            self.add_message(placeholder, include_in_context=True)

    def load_message(self) -> list[BaseMessage]:
        """返回 system prompt 加 Task context 的模型输入副本。

        取数前先统一闭合未配对的工具调用占位（崩溃/取消遗留），保证返回给模型的
        上下文协议闭合。

        副作用:
            若检测到悬空调用，经 ``_close_unclosed_tool_calls`` -> ``add_message`` 补占位会
            把 ``have_change`` 置为 True，取数即可能触发上下文持久化与变更通知；无悬空时
            保持原 ``have_change=False``。
        """
        self.have_change = False
        self._close_unclosed_tool_calls()
        return [entry.message for entry in copy.deepcopy(self._effective_entries())]

    def _effective_entries(self) -> list[ContextEntry]:
        """返回 system、历史和当前 run 条目的有序列表。"""

        if self._system_entry is None:
            raise RuntimeError("system entry is not initialized")
        return [self._system_entry, *self._entries]

    def has_change(self) -> bool:
        """是否有上下文变化。"""

        return self.have_change

    def mark_context_changed(
            self,
            event_type: ContextEventType,
            entries: list[ContextEntry],
    ) -> None:
        """向 listener 发布 context 完整快照。"""

        result = ListenerResult(self.used_tokens)
        snapshot = copy.deepcopy(entries)
        for listener in self._listeners:
            listener.listen(
                ListenerEvent(
                    event_type,
                    snapshot,
                    self.used_tokens,
                    self.total_tokens,
                ),
                result,
            )
        self.used_tokens = result.usage
