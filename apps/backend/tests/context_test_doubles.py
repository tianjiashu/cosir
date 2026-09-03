"""上下文子系统测试的共享替身与记录工厂。

只提供 ``RuntimeContextManager`` 与 context listener 测试复用的三类构件：内存消息
仓库替身（``RuntimeMessageStore`` 协议形状）、task / turn / workspace 记录工厂、
listener 测试替身。不承载任何断言逻辑，避免测试间隐式耦合。

当前被 ``test_context_usage_listener`` 与 ``test_context_listener_entries`` 复用。
测试文件不得再各自复制这些构件——需要新能力时在此扩展。
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime

from app.core.agents.agent_profile import AgentProfile
from app.core.context.context_entry import ContextEntry
from app.core.context.context_listener.context_listener import ContextListener
from app.core.context.context_listener.listener_event import ListenerEvent
from app.core.context.context_listener.listener_result import ListenerResult
from app.models import RuntimeMessage, TaskRecord, ConversationRunRecord, WorkspaceRecord


class MemoryMessageStore:
    """测试用内存消息仓库，保留 ``RuntimeContextManager`` 依赖的真实协议形状。"""

    def __init__(self, history: list[RuntimeMessage] | None = None) -> None:
        """初始化测试仓库。

        参数:
            history: 按 task 维度返回的历史消息。

        返回:
            无。

        异常:
            无。

        副作用:
            保存历史消息与 append/clear 调用记录。
        """
        self.history = history or []
        self.appended: list[tuple[int, RuntimeMessage, int, bool]] = []
        self.cleared: list[int] = []

    def append(
        self,
        run_id: int,
        message: RuntimeMessage,
        sequence: int,
        include_in_context: bool = True,
    ) -> None:
        """记录追加消息调用。

        参数:
            run_id: 目标 turn 标识。
            message: 被追加的运行时消息。
            sequence: 轮内序号。
            include_in_context: 是否进入模型上下文。

        返回:
            无。

        异常:
            无。

        副作用:
            写入 ``appended`` 调用记录。
        """
        self.appended.append((run_id, message, sequence, include_in_context))

    def clear(self, run_id: int) -> None:
        """记录清理消息调用。

        参数:
            run_id: 目标 turn 标识。

        返回:
            无。

        异常:
            无。

        副作用:
            写入 ``cleared`` 调用记录。
        """
        self.cleared.append(run_id)

    def build_for_task(
        self,
        task_id: int,
        excluded_run_ids: Collection[int] | None = None,
    ) -> list[ContextEntry]:
        """返回预置历史消息（包装为 ContextEntry，模拟真实 store 契约）。

        参数:
            task_id: 目标 task 标识。
            excluded_run_ids: 需要排除的 turn 标识集合；测试仓库不按 turn 分组时忽略。

        返回:
            预置历史消息副本（ContextEntry 列表）。

        异常:
            无。

        副作用:
            无。
        """
        return [ContextEntry(message=message, run_id=None) for message in self.history]

    def build_for_run(self, run_id: int) -> list[ContextEntry]:
        """返回当前 turn 的预置历史消息（包装为 ContextEntry）。

        参数:
            run_id: 目标 turn 标识。

        返回:
            预置历史消息副本（ContextEntry 列表）。

        异常:
            无。

        副作用:
            无。
        """
        return [ContextEntry(message=message, run_id=run_id) for message in self.history]

    def next_run_sequence(self, run_id: int) -> int:
        """返回测试仓库下一条消息序号。

        参数:
            run_id: 目标 turn 标识。

        返回:
            历史消息条数。

        异常:
            无。

        副作用:
            无。
        """
        return len(self.history)


class TurnScopedMemoryMessageStore(MemoryMessageStore):
    """按 turn 返回历史的测试仓库，用于验证当前 turn 排除语义。"""

    def __init__(self, histories: dict[int, list[RuntimeMessage]]) -> None:
        """初始化按 turn 分组的测试仓库。

        参数:
            histories: turn 标识到消息列表的映射。

        返回:
            无。

        异常:
            无。

        副作用:
            保存按 turn 分组的历史消息。
        """
        super().__init__()
        self.histories = histories

    def build_for_task(
        self,
        task_id: int,
        excluded_run_ids: Collection[int] | None = None,
    ) -> list[ContextEntry]:
        """返回排除指定 turn 后的有序历史（包装为 ContextEntry）。

        参数:
            task_id: 目标 task 标识。
            excluded_run_ids: 需要排除的 turn 标识集合。

        返回:
            按 turn 标识顺序拼接的历史消息副本（ContextEntry 列表）。

        异常:
            无。

        副作用:
            无。
        """
        excluded = set(excluded_run_ids or ())
        return [
            ContextEntry(message=message, run_id=run_id)
            for run_id, messages in sorted(self.histories.items())
            if run_id not in excluded
            for message in messages
        ]

    def build_for_run(self, run_id: int) -> list[ContextEntry]:
        """返回指定 turn 的有效上下文消息（包装为 ContextEntry）。

        参数:
            run_id: 目标 turn 标识。

        返回:
            该 turn 的消息副本（ContextEntry 列表）。

        异常:
            无。

        副作用:
            无。
        """
        return [
            ContextEntry(
                message=RuntimeMessage(
                    role=message.role,
                    content_text=message.content_text,
                    content_blocks=message.content_blocks,
                    metadata=message.metadata,
                ),
                run_id=run_id,
            )
            for message in self.histories.get(run_id, [])
        ]

    def next_run_sequence(self, run_id: int) -> int:
        """返回指定 turn 的下一条消息序号。

        参数:
            run_id: 目标 turn 标识。

        返回:
            该 turn 的消息条数；无该 turn 时返回 0。

        异常:
            无。

        副作用:
            无。
        """
        return len(self.histories.get(run_id, []))


def build_agent_profile(*, main_agent: bool) -> AgentProfile:
    """构造测试用 AgentProfile。

    参数:
        main_agent: 是否标记为主 Agent。

    返回:
        测试用 AgentProfile。

    异常:
        无。

    副作用:
        无。
    """
    return AgentProfile(
        agent_id="main_agent" if main_agent else "delegate_reviewer",
        role="test",
        description="test",
        allowed_tools=[],
        main_agent=main_agent,
    )


def build_task_record(task_id: int = 7) -> TaskRecord:
    """构造测试用 TaskRecord。

    参数:
        task_id: task 标识。

    返回:
        测试用 TaskRecord。

    异常:
        无。

    副作用:
        无。
    """
    now = datetime.now(UTC)
    return TaskRecord(
        id=task_id,
        workspace_id=1,
        title="test",
        status="open",
        created_at=now,
        updated_at=now,
    )


def build_workspace_record() -> WorkspaceRecord:
    """构造测试用 WorkspaceRecord。

    参数:
        无。

    返回:
        测试用 WorkspaceRecord。

    异常:
        无。

    副作用:
        无。
    """
    now = datetime.now(UTC)
    return WorkspaceRecord(
        id=1,
        name="test",
        root_path="H:/coding-agent",
        created_at=now,
        updated_at=now,
    )


def build_conversation_run_record(run_id: int = 11, task_id: int = 7) -> ConversationRunRecord:
    """构造测试用 ConversationRunRecord。

    参数:
        run_id: turn 标识。
        task_id: 所属 task 标识。

    返回:
        测试用 ConversationRunRecord。

    异常:
        无。

    副作用:
        无。
    """
    now = datetime.now(UTC)
    return ConversationRunRecord(
        id=run_id,
        task_id=task_id,
        input_text="hello",
        status="running",
        created_at=now,
        updated_at=now,
        model_name="deepseek/deepseek-v4-flash",
    )


def build_conversation_run_record_for_task(task_id: int, run_id: int) -> ConversationRunRecord:
    """构造绑定指定 task 的测试用 ConversationRunRecord。

    参数:
        task_id: 所属 task 标识。
        run_id: turn 标识。

    返回:
        测试用 ConversationRunRecord。

    异常:
        无。

    副作用:
        无。
    """
    turn = build_conversation_run_record(run_id=run_id)
    turn.task_id = task_id
    return turn


class RecordingListener(ContextListener):
    """记录收到的事件，供调用方断言事件载体形状。"""

    main_agent_only = False
    order = 0

    def __init__(self, sink: list[ListenerEvent]) -> None:
        """绑定事件收集容器。

        参数:
            sink: 追加收到事件的列表（调用方持有）。

        返回:
            无。

        异常:
            无。

        副作用:
            保存容器引用。
        """
        self._sink = sink

    def listen(self, event: ListenerEvent, result: ListenerResult) -> None:
        """把事件追加进容器，不修改 ``result``。

        参数:
            event: 监听事件。
            result: 累计结果；本实现不修改它。

        返回:
            无。

        异常:
            无。

        副作用:
            向构造时注入的容器追加一条事件。
        """
        self._sink.append(event)


class ThrowingListener(ContextListener):
    """在 :meth:`listen` 中写入 usage 并可选抛异常，验证 ``finally`` 覆盖语义。"""

    main_agent_only = False
    order = 0

    def __init__(self, usage_value: int, exc: BaseException | None = None) -> None:
        """保存要写入的 usage 与可选异常。

        参数:
            usage_value: 写入 ``result.usage`` 的值。
            exc: 非 None 时写入 usage 后抛出。

        返回:
            无。

        异常:
            无。

        副作用:
            保存 usage 值与异常对象。
        """
        self._usage_value = usage_value
        self._exc = exc

    def listen(self, event: ListenerEvent, result: ListenerResult) -> None:
        """写入 usage 后按需抛异常。

        参数:
            event: 监听事件（未使用）。
            result: 累计结果，``usage`` 被覆盖为构造时给定的值。

        返回:
            无。

        异常:
            BaseException: 构造时注入的异常，原样抛出。

        副作用:
            覆盖 ``result.usage``。
        """
        result.usage = self._usage_value
        if self._exc is not None:
            raise self._exc


class ClearingListener(ContextListener):
    """清空收到的事件快照列表，用于验证快照与 manager 内部状态隔离。"""

    main_agent_only = False
    order = 0

    def listen(self, event: ListenerEvent, result: ListenerResult) -> None:
        """清空快照条目列表，不修改 manager。

        参数:
            event: 监听事件（快照条目被清空）。
            result: 累计结果；本实现不修改它。

        返回:
            无。

        异常:
            无。

        副作用:
            清空 ``event.entries``。
        """
        event.entries.clear()


class MutableSubObjectListener(ContextListener):
    """污染快照条目的可变子对象，验证深拷贝而非浅拷贝的隔离契约。

    ``RuntimeMessage`` 是 frozen dataclass，但 ``metadata`` / ``content_blocks`` 是可变
    容器：只有深拷贝能阻止本替身的修改回灌 manager 内部条目。
    """

    main_agent_only = False
    order = 0

    def listen(self, event: ListenerEvent, result: ListenerResult) -> None:
        """向快照条目的 ``metadata`` 与 ``content_blocks`` 注入数据。

        参数:
            event: 监听事件（快照条目被污染）。
            result: 累计结果；本实现不修改它。

        返回:
            无。

        异常:
            无。

        副作用:
            修改事件快照中各条目消息的可变子对象。
        """
        for entry in event.entries:
            entry.message.metadata["injected"] = "snapshot-only"
            if entry.message.content_blocks is not None:
                entry.message.content_blocks.append({"type": "text", "text": "injected"})
