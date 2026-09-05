"""Conversation event 到 Transport snapshot projector 的回归测试。"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app.assistant_transport.service.conversation_event_projector import (
    ConversationEventProjector,
)
from app.assistant_transport.service.conversation_task_snapshot_service import SnapshotChange
from app.assistant_transport.service.transport_assistant_service import (
    TransportAssistantService,
)
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    empty_snapshot,
    validate_snapshot,
)
from app.core.workflows.event import (
    AssistantPartClosedEvent,
    AssistantTextDeltaEvent,
    ContextUsageUpdatedEvent,
    RunInitializedEvent,
    RunStatusChangedEvent,
    ToolCallCreatedEvent,
    ToolCallsSettledEvent,
    ToolCallStatusChangedEvent,
    UsageUpdatedEvent,
    UserInputAppendedEvent,
)
from app.models.enums.conversation_run_status import ConversationRunStatus


class InMemorySnapshotService:
    """测试用 snapshot owner，复用生产 mutation 语义但不触碰 SQLite。"""

    def __init__(self) -> None:
        self.states: dict[int, ConversationStateSnapshot] = {}

    def ensure_state_snapshot(self, task_id: int) -> ConversationStateSnapshot:
        state = self.states.setdefault(task_id, empty_snapshot())
        return copy.deepcopy(state)

    def apply_planned(self, task_id: int, planner: Any) -> SnapshotChange:
        state = self.ensure_state_snapshot(task_id)
        mutations = tuple(planner(copy.deepcopy(state)))
        for mutation in mutations:
            _apply_mutation(state, mutation)
        validate_snapshot(state)
        self.states[task_id] = copy.deepcopy(state)
        return SnapshotChange(task_id, copy.deepcopy(state), mutations)


def _apply_mutation(state: ConversationStateSnapshot, mutation: ConversationStateMutation) -> None:
    """在测试内应用生产 snapshot mutation。"""

    parent: Any = state
    if not mutation.path:
        parent.clear()
        parent.update(copy.deepcopy(mutation.value))
        return
    for key in mutation.path[:-1]:
        parent = parent[key]
    key = mutation.path[-1]
    if mutation.kind == "set":
        if isinstance(parent, list) and key == len(parent):
            parent.append(copy.deepcopy(mutation.value))
        else:
            parent[key] = copy.deepcopy(mutation.value)
    else:
        parent[key] += mutation.value


@pytest.fixture
def projector() -> tuple[ConversationEventProjector, InMemorySnapshotService]:
    snapshots = InMemorySnapshotService()
    return ConversationEventProjector(snapshots), snapshots


def _start(projector: ConversationEventProjector) -> None:
    projector.process(RunInitializedEvent(task_id=1, run_id=1))


def test_run_initialized_and_user_input(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(UserInputAppendedEvent(task_id=1, run_id=1, text="你好"))
    state = snapshots.states[1]
    assert state["run"] == {"runId": 1, "status": "pending"}
    assert state["messages"][0]["parts"][0]["text"] == "你好"


def test_assistant_text_and_reasoning_parts(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(
        AssistantTextDeltaEvent(task_id=1, run_id=1, part="reasoning", delta="先思考")
    )
    event_projector.process(
        AssistantTextDeltaEvent(task_id=1, run_id=1, part="reasoning", delta="一下")
    )
    event_projector.process(AssistantPartClosedEvent(task_id=1, run_id=1, part="reasoning"))
    event_projector.process(AssistantTextDeltaEvent(task_id=1, run_id=1, part="text", delta="答案"))
    parts = snapshots.states[1]["messages"][1]["parts"]
    assert parts[1]["text"] == "先思考一下"
    assert parts[1]["status"] == "completed"
    assert parts[0]["text"] == "答案"


def test_duplicate_text_delta_is_not_appended_twice(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event = AssistantTextDeltaEvent(task_id=1, run_id=1, part="text", delta="一次")
    first = event_projector.process(event)
    second = event_projector.process(event)
    assert first is not None and first.mutations
    assert second is not None and second.mutations == ()
    assert snapshots.states[1]["messages"][1]["parts"][0]["text"] == "一次"


def test_tool_lifecycle_and_settlement(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(
        ToolCallCreatedEvent(
            task_id=1, run_id=1, tool_call_id="call-1", tool_name="read_file", args={"path": "a.py"}
        )
    )
    event_projector.process(
        ToolCallStatusChangedEvent(task_id=1, run_id=1, tool_call_id="call-1", status="running")
    )
    event_projector.process(
        ToolCallStatusChangedEvent(
            task_id=1, run_id=1, tool_call_id="call-1", status="completed", result="内容"
        )
    )
    event_projector.process(
        ToolCallCreatedEvent(
            task_id=1, run_id=1, tool_call_id="call-2", tool_name="write_file", args={}
        )
    )
    event_projector.process(
        ToolCallsSettledEvent(task_id=1, run_id=1, status="failed", reason="runtime_failed")
    )
    parts = snapshots.states[1]["messages"][1]["parts"]
    assert parts[1]["status"] == "completed"
    assert parts[1]["result"] == "内容"
    assert parts[2]["status"] == "failed"
    assert parts[2]["error"] == "runtime_failed"


def test_run_status_usage_and_context_usage(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(
        UsageUpdatedEvent(task_id=1, run_id=1, input_tokens=10, output_tokens=4, total_tokens=14)
    )
    event_projector.process(
        ContextUsageUpdatedEvent(task_id=1, run_id=1, ratio=0.42, used_tokens=42)
    )
    event_projector.process(
        RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.COMPLETED)
    )
    state = snapshots.states[1]
    assert state["usage"]["total_tokens"] == 14
    assert state["context_usage"] == 0.42
    assert state["run"]["status"] == "completed"
    assert state["messages"][1]["status"] == "completed"


def test_unknown_event_is_ignored(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    assert event_projector.process({"type": "future_event", "task_id": 1, "run_id": 1}) is None
    assert snapshots.states == {}


def test_model_chunk_event_becomes_assistant_transport_update() -> None:
    """验证 model chunk → event → mutation → Transport state adapter 的闭环。"""

    snapshots = InMemorySnapshotService()
    event_projector = ConversationEventProjector(snapshots)
    _start(event_projector)
    before = copy.deepcopy(snapshots.states[1])
    event = AssistantTextDeltaEvent(task_id=1, run_id=1, part="text", delta="你好")
    change = event_projector.process(event)
    assert change is not None and change.mutations

    class Controller:
        def __init__(self, state: ConversationStateSnapshot) -> None:
            self.state = state
            self.appended: list[tuple[list[str | int], str]] = []

        def append_state_text(self, path: list[str | int], value: str) -> None:
            self.appended.append((path, value))

    controller = Controller(before)
    for mutation in change.mutations:
        TransportAssistantService._apply_state_mutation(  # type: ignore[arg-type]
            TransportAssistantService.__new__(TransportAssistantService), controller, mutation
        )
    assert controller.appended == [(["messages", 1, "parts", 0, "text"], "你好")]
