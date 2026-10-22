"""工具结果投影与 context 配对的内存测试。"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.assistant_transport.projection.event_projection import plan_event
from app.assistant_transport.service.conversation_event_projector import (
    ConversationEventProjector,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    empty_snapshot,
    validate_snapshot,
)
from app.assistant_transport.stream import TransportFrame
from app.core.context.context_entry import ContextEntry
from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager
from app.core.events import (
    RunInitializedEvent,
    RunStatusChangedEvent,
    TerminalOutputDeltaData,
    ToolCallCreatedEvent,
    ToolCallRuntimeUpdateEvent,
    ToolCallStatusChangedEvent,
    UserInputConsumedEvent,
)
from app.core.tools.schemas import ToolObservation
from app.models.conversation_input import UserInputPresentation
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.message_metadata import ToolMessageMetadata

_TASK_ID = 7
_RUN_ID = 9


# ============================================================================
# 共享装配
# ============================================================================


class _FakeStreamWriter:
    """收集工具链路事件的假 stream writer。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def __call__(self, event: Any) -> None:
        self.events.append(event)

    @property
    def status_events(self) -> list[ToolCallStatusChangedEvent]:
        return [event for event in self.events if isinstance(event, ToolCallStatusChangedEvent)]


def _observation(**overrides: Any) -> ToolObservation:
    """构造最小 ``ToolObservation``（供假工具批次执行返回）。"""

    base: dict[str, Any] = {
        "tool_name": "read_file",
        "status": "error",
        "content": None,
        "error": "denied",
        "reason": "denied",
        "retryable": False,
        "tool_call_id": "call-1",
        "display_data": {},
    }
    base.update(overrides)
    return ToolObservation(**base)


def _summary(**overrides: Any) -> dict[str, Any]:
    """构造最小观察摘要（``tools`` 节点 ``dataclasses.asdict`` 投影的形状）。"""

    base: dict[str, Any] = {
        "tool_call_id": "call-1",
        "tool_name": "read_file",
        "status": "success",
        "error": "",
        "reason": "",
        "content": "ok",
        "retryable": False,
        "display_data": {},
    }
    base.update(overrides)
    return base


class _MemorySnapshotOwner:
    """最小 snapshot owner，按 projector mutations 原地更新测试状态。"""

    def __init__(self, state: ConversationStateSnapshot) -> None:
        self.state = state

    def get_state(self, _task_id: int) -> ConversationStateSnapshot:
        return copy.deepcopy(self.state)

    def refresh_parent_delegation(self, _child_task_id: int, _run_id: int, _status: str) -> None:
        """单任务内存 owner 没有父委派卡片可同步；生产实现见 ConversationTaskStateService。"""
        return None

    def apply_planned(self, event: Any) -> TransportFrame:
        mutations = tuple(plan_event(event, copy.deepcopy(self.state)))
        for mutation in mutations:
            parent: Any = self.state
            for key in mutation.path[:-1]:
                parent = parent[key]
            key = mutation.path[-1]
            if mutation.kind == "append-text":
                parent[key] += mutation.value
            elif isinstance(parent, list) and key == len(parent):
                parent.append(copy.deepcopy(mutation.value))
            else:
                parent[key] = copy.deepcopy(mutation.value)
        validate_snapshot(self.state)
        return TransportFrame(
            task_id=event.task_id,
            kind="mutation",
            mutations=mutations,
            source_run_id=getattr(event, "run_id", None),
        )


def _seed_tool_part(tool_call_id: str = "call-1") -> tuple[ConversationStateSnapshot, Any]:
    """建立 run + assistant 骨架 + 一个 ``running`` 的 tool-call part，并注入流式输出。"""

    state = empty_snapshot()
    projector = ConversationEventProjector(_MemorySnapshotOwner(state))
    projector.process(RunInitializedEvent(task_id=_TASK_ID, run_id=_RUN_ID))
    projector.process(
        UserInputConsumedEvent(
            task_id=_TASK_ID,
            run_id=_RUN_ID,
            message_id="u1",
            presentation=UserInputPresentation(display_text="test"),
        )
    )
    projector.process(RunStatusChangedEvent(task_id=_TASK_ID, run_id=_RUN_ID, status="running"))
    projector.process(
        ToolCallCreatedEvent(
            task_id=_TASK_ID,
            run_id=_RUN_ID,
            tool_call_id=tool_call_id,
            tool_name="execute_terminal",
        )
    )
    projector.process(
        ToolCallStatusChangedEvent(
            task_id=_TASK_ID, run_id=_RUN_ID, tool_call_id=tool_call_id, status="running"
        )
    )
    projector.process(
        ToolCallRuntimeUpdateEvent(
            task_id=_TASK_ID,
            run_id=_RUN_ID,
            tool_call_id=tool_call_id,
            seq=0,
            data=TerminalOutputDeltaData(kind="terminal_output_delta", text="streamed"),
        )
    )
    return state, projector


def _tool_part(state: ConversationStateSnapshot) -> dict[str, Any]:
    """取 assistant 消息里唯一的 tool-call part。"""

    for message in state["runs"][0]["messages"]:
        for part in message["parts"]:
            if isinstance(part, dict) and part.get("type") == "tool-call":
                return part
    raise AssertionError("tool-call part 不存在")


# ============================================================================
# P1. ``_tools_node`` 重入：pending 记录是否被二次执行
# ============================================================================


# ============================================================================
# P2. 终态事件是否清空已流出的终端输出（按 display_data.kind 分形状）
# ============================================================================


def test_p2_terminal_result_keeps_streamed_output() -> None:
    """P2-a：``kind == "terminal-result"`` 且新载荷同为 terminal-result 时，输出**被保留**。

    被测行为：``ToolCallStatusChangedEvent.plan`` 的 ``has_streamed_terminal_output`` 补偿分支。
    缺陷判定：符合预期（补偿分支生效）。
    """

    state, projector = _seed_tool_part()
    assert _tool_part(state)["display_data"] == {"kind": "terminal-result", "output": "streamed"}

    projector.process(
        ToolCallStatusChangedEvent(
            task_id=_TASK_ID,
            run_id=_RUN_ID,
            tool_call_id="call-1",
            status="completed",
            display_data={"kind": "terminal-result", "output": "", "exit_code": 0},
        )
    )

    part = _tool_part(state)
    assert part["display_data"]["output"] == "streamed", f"实际={part['display_data']}"
    assert part["display_data"]["exit_code"] == 0


def test_p2_empty_display_data_keeps_streamed_terminal_output() -> None:
    """P2-b：终态事件的 ``display_data`` 为空 ``dict``（取消 / 失败观察的默认形态）时，已流出的
    终端输出保留，终态只叠加自带字段。"""

    state, projector = _seed_tool_part()
    assert _tool_part(state)["display_data"]["output"] == "streamed"

    projector.process(
        ToolCallStatusChangedEvent(
            task_id=_TASK_ID,
            run_id=_RUN_ID,
            tool_call_id="call-1",
            status="cancelled",
            display_data={},
        )
    )

    part = _tool_part(state)
    assert part["status"] == "cancelled"
    assert part["display_data"]["output"] == "streamed"


def test_p2_explicit_kind_display_replaces_streamed_terminal_output() -> None:
    """P2-c：带明确 ``kind`` 的终态展示是权威结果，照常整体替换，不与旧的 terminal-result 拼接。"""

    state, projector = _seed_tool_part()

    projector.process(
        ToolCallStatusChangedEvent(
            task_id=_TASK_ID,
            run_id=_RUN_ID,
            tool_call_id="call-1",
            status="failed",
            display_data={"kind": "terminal-session", "status_hint": "执行失败"},
        )
    )

    part = _tool_part(state)
    assert part["display_data"] == {
        "kind": "terminal-session",
        "status_hint": "执行失败",
    }, f"实际={part['display_data']}"
    assert "output" not in part["display_data"]


# ============================================================================
# P4. ToolMessage 顺序 vs AIMessage.tool_calls 顺序
# ============================================================================


class _RecordingContextService:
    """记录 append / replace 的最小 context service（不落真实数据库）。"""

    def __init__(self, max_sequence: int = 0) -> None:
        self.max_sequence_value = max_sequence
        self.appended: list[tuple[int, int | None, Any, int, bool]] = []
        self.replaced: list[ConversationTaskContextRecord] = []
        self.loaded: list[ContextEntry] = []
        self.deleted: list[Any] = []

    def max_sequence(self, task_id: int) -> int:
        return self.max_sequence_value

    def entries_in_context(self, task_id: int) -> list[ContextEntry]:
        return list(self.loaded)

    def get_system_prompt(self, task_id: int) -> ContextEntry | None:
        return None

    def create_system_prompt(self, task_id: int, message: Any) -> ContextEntry:
        return ContextEntry(message, None, -1)

    def append(
        self,
        task_id: int,
        run_id: int | None,
        message: Any,
        sequence: int,
        include_in_context: bool,
        metadata: Any = None,
        project_to_transport: bool = True,
    ) -> None:
        self.appended.append((task_id, run_id, message, sequence, include_in_context))
        return ContextEntry(message, run_id, sequence)

    def replace_message(self, record: Any) -> None:
        self.replaced.append(record)

    def delete_by_run_id(self, task_id: int, run_id: int) -> None:
        self.deleted.append(run_id)


def _memory_manager(
    service: _RecordingContextService, next_sequence: int = 1
) -> RuntimeMessageContextManager:
    """构造绕过真实持久化的 ``RuntimeMessageContextManager``（与既有测试同口径）。"""

    manager = object.__new__(RuntimeMessageContextManager)
    manager.current_task_id = _TASK_ID
    manager.agent_profile = SimpleNamespace()
    manager.workspace = SimpleNamespace(id=7, root_path="")
    manager.context_service = service
    manager.current_run_id = _RUN_ID
    manager._system_entry = ContextEntry({"role": "system", "content": "system"}, None, -1)
    manager._entries = []
    manager._message_sequence = next_sequence
    return manager


def test_p4_memory_reorders_but_persisted_order_is_never_fixed() -> None:
    """P4-b：``load_message`` 只在**内存**里重排 ToolMessage，数据库落库顺序永不修正。

    被测行为：AI(tool_calls=[blocked-1, valid-1]) 之后先落 valid-1、再落 blocked-1；
    ``_close_unclosed_tool_calls`` 把内存工作副本重排为 tool_calls 顺序，但
    ``context_service`` 只收到 append，没有任何改写顺序的调用。
    缺陷判定：低危（provider 按 id 配对；仅库内 / 冷读顺序与 tool_calls 顺序不一致）。
    """

    service = _RecordingContextService(max_sequence=0)
    manager = _memory_manager(service, next_sequence=1)

    manager.add_message(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "blocked-1",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": json.dumps({})},
                },
                {
                    "id": "valid-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": json.dumps({})},
                },
            ],
        }
    )
    # 落库顺序故意与 tool_calls 相反（valid 先、blocked 后）。
    manager.add_message(
        {"role": "tool", "content": "valid result", "tool_call_id": "valid-1"},
        metadata=ToolMessageMetadata(status="completed"),
    )
    manager.add_message(
        {"role": "tool", "content": "disabled", "tool_call_id": "blocked-1"},
        metadata=ToolMessageMetadata(status="completed"),
    )

    persisted_before = [message.get("tool_call_id") for _, _, message, _, _ in service.appended]
    assert persisted_before == [None, "valid-1", "blocked-1"], f"落库顺序={persisted_before}"

    loaded = manager.load_message()
    tool_messages = [message.get("tool_call_id") for message in loaded if message["role"] == "tool"]
    # 内存工作副本被重排为 tool_calls 顺序。
    assert tool_messages == ["blocked-1", "valid-1"], f"内存顺序={tool_messages}"
    # 数据库侧没有任何改写：append 记录不变，replace 为空。
    persisted_after = [message.get("tool_call_id") for _, _, message, _, _ in service.appended]
    assert persisted_after == persisted_before, "数据库落库顺序未被修正（缺陷：永不修正）"
    assert service.replaced == []


# ============================================================================
# P5. 崩溃占位被真实结果覆盖（resume 场景）
# ============================================================================


def test_p5_placeholder_is_replaced_by_real_result_after_resume() -> None:
    """P5-a：B 崩溃 → 补 ``cancelled`` 占位 → 真实结果到达时**原地覆盖**，不并排留第二行。

    被测行为：占位写入时携带 ``run_id=plan.target_run_id``，与 resume 后的
    ``current_run_id`` 相同，因此 ``add_message`` 的 ``candidate.run_id == target_run_id``
    判定成立，走 ``replace_message`` 分支。
    缺陷判定：符合预期（不是缺陷）。
    """

    service = _RecordingContextService(max_sequence=0)
    manager = _memory_manager(service, next_sequence=1)

    manager.add_message(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "A",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": json.dumps({})},
                },
                {
                    "id": "B",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": json.dumps({})},
                },
            ],
        }
    )
    manager.add_message(
        {"role": "tool", "content": "A result", "tool_call_id": "A"},
        metadata=ToolMessageMetadata(status="completed"),
    )

    # 模拟进程崩溃后 resume：load_message 为未配对的 B 补 cancelled 占位。
    loaded = manager.load_message()
    tool_ids = [message.get("tool_call_id") for message in loaded if message["role"] == "tool"]
    assert tool_ids == ["A", "B"], f"补占位后的顺序={tool_ids}"
    assert "did not produce a result" in str(loaded[-1].get("content")), "B 应得到 cancelled 占位"

    # 真实结果到达。
    outcome = manager.add_message(
        {"role": "tool", "content": "real B result", "tool_call_id": "B"},
        metadata=ToolMessageMetadata.model_validate({"status": "completed"}),
    )

    assert outcome == "replaced", "占位必须被原地覆盖，而不是并排追加第二行"
    assert len(service.replaced) == 1
    assert [entry.message.get("content") for entry in manager._entries] == [
        "",
        "A result",
        "real B result",
    ]
    # 总行数没有增加：只有 AI / A / B 三行（B 那行被原地替换）。
    assert len(manager._entries) == 3


def test_p5_placeholder_of_another_run_is_not_overwritten() -> None:
    """跨 Run 结果不能覆盖旧行，也不能在没有新调用依据时追加成孤儿。"""
    from app.core.context.tool_call_closure import ContextProtocolError

    service = _RecordingContextService(max_sequence=0)
    manager = _memory_manager(service, next_sequence=1)
    manager.current_run_id = _RUN_ID

    manager.add_message(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "A",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": json.dumps({})},
                }
            ],
        }
    )
    manager.add_message(
        {"role": "tool", "content": "old run result", "tool_call_id": "A"},
        metadata=ToolMessageMetadata(status="completed"),
    )

    with pytest.raises(ContextProtocolError, match="tool_result_without_current_call"):
        manager.add_message(
            {"role": "tool", "content": "new run result", "tool_call_id": "A"},
            run_id=_RUN_ID + 1,
            metadata=ToolMessageMetadata(status="completed"),
        )
    assert len(manager._entries) == 2
    assert manager._entries[-1].message["content"] == "old run result"
    assert service.replaced == []


# ============================================================================
# P6. ``_observe_node`` 在 run 已取消时是否仍执行一整轮结算
# ============================================================================
