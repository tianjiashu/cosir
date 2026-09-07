"""Conversation event 到 Transport snapshot projector 的回归测试。"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

from app.assistant_transport.service.conversation_event_projector import (
    ConversationEventProjector,
)
from app.assistant_transport.service.conversation_task_snapshot_service import (
    ConversationTaskSnapshotService,
    SnapshotChange,
)
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
    assert state["messages"][1]["parts"] == []


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
    assert [part["type"] for part in parts] == ["reasoning", "text"]
    assert parts[0]["text"] == "先思考一下"
    assert parts[0]["status"] == "completed"
    assert parts[1]["text"] == "答案"


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
            task_id=1,
            run_id=1,
            tool_call_id="call-1",
            tool_name="read_file",
            args={"path": "a.py"},
            presentation={
                "verb": "读取文件",
                "icon": "eye",
                "surface": "trace",
                "expandable": False,
                "expand_layout": "none",
                "default_open": False,
            },
        )
    )
    event_projector.process(
        ToolCallStatusChangedEvent(task_id=1, run_id=1, tool_call_id="call-1", status="running")
    )
    event_projector.process(
        ToolCallStatusChangedEvent(
            task_id=1,
            run_id=1,
            tool_call_id="call-1",
            status="completed",
            result="内容",
            data={"kind": "read-file-meta", "path": "a.py", "total_lines": 10},
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
    assert parts[0]["status"] == "completed"
    assert parts[0]["presentation"]["expandable"] is False
    assert parts[0]["result"] == "内容"
    assert parts[0]["data"] == {"kind": "read-file-meta", "path": "a.py", "total_lines": 10}
    assert parts[1]["status"] == "failed"
    assert parts[1]["error"] == "runtime_failed"


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
            self.flush_count = 0

        def append_state_text(self, path: list[str | int], value: str) -> None:
            self.appended.append((path, value))

        def flush(self) -> None:
            self.flush_count += 1

    controller = Controller(before)
    snapshot_service = ConversationTaskSnapshotService.__new__(ConversationTaskSnapshotService)
    snapshot_service._apply_snapshot_change(controller, change)
    assert controller.appended == []
    assert controller.state["messages"][1]["parts"] == [
        {"type": "text", "text": "你好", "status": "running"}
    ]
    assert controller.flush_count == 1


@pytest.mark.asyncio
async def test_stream_delivers_queued_terminal_change_before_exit() -> None:
    """终态轮询命中时，仍必须先消费已经排队的终态快照。"""

    initial = empty_snapshot()
    initial["run"] = {"runId": 1, "status": "running"}
    terminal = copy.deepcopy(initial)
    terminal["run"] = {"runId": 1, "status": "completed"}
    queue: asyncio.Queue[SnapshotChange] = asyncio.Queue()

    class Snapshots:
        def subscribe(self, task_id: int) -> tuple[asyncio.Queue[SnapshotChange], Any]:
            return queue, lambda: None

        def ensure_state_snapshot(self, task_id: int) -> ConversationStateSnapshot:
            return copy.deepcopy(initial)

    service = TransportAssistantService.__new__(TransportAssistantService)
    service._snapshots = Snapshots()

    stream = service.stream(1, 1, lambda: False, is_terminal=lambda: _true())
    first = await anext(stream)
    assert first.state["run"]["status"] == "running"
    await queue.put(SnapshotChange(1, terminal, ()))
    second = await anext(stream)
    assert second.state["run"]["status"] == "completed"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_stream_waits_for_terminal_snapshot_projection() -> None:
    """run 已终态但 snapshot 尚未投影时，订阅继续等待最后一帧。"""

    initial = empty_snapshot()
    initial["run"] = {"runId": 1, "status": "running"}
    terminal = copy.deepcopy(initial)
    terminal["run"] = {"runId": 1, "status": "completed"}
    queue: asyncio.Queue[SnapshotChange] = asyncio.Queue()

    class Snapshots:
        def subscribe(self, task_id: int) -> tuple[asyncio.Queue[SnapshotChange], Any]:
            return queue, lambda: None

        def ensure_state_snapshot(self, task_id: int) -> ConversationStateSnapshot:
            return copy.deepcopy(initial)

    async def delayed_terminal_change() -> None:
        await asyncio.sleep(0.06)
        await queue.put(SnapshotChange(1, terminal, ()))

    service = TransportAssistantService.__new__(TransportAssistantService)
    service._snapshots = Snapshots()
    stream = service.stream(1, 1, lambda: False, is_terminal=lambda: _true(), poll_interval=0.05)
    await anext(stream)
    terminal_task = asyncio.create_task(delayed_terminal_change())
    change = await anext(stream)
    assert change.state["run"]["status"] == "completed"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    await terminal_task


@pytest.mark.asyncio
async def test_stream_does_not_close_while_run_is_still_active() -> None:
    """没有 snapshot 通知时，活跃 run 的 SSE 不能被 idle timeout 提前关闭。"""

    initial = empty_snapshot()
    initial["run"] = {"runId": 1, "status": "running"}
    queue: asyncio.Queue[SnapshotChange] = asyncio.Queue()

    class Snapshots:
        def subscribe(self, task_id: int) -> tuple[asyncio.Queue[SnapshotChange], Any]:
            return queue, lambda: None

        def ensure_state_snapshot(self, task_id: int) -> ConversationStateSnapshot:
            return copy.deepcopy(initial)

    service = TransportAssistantService.__new__(TransportAssistantService)
    service._snapshots = Snapshots()

    async def delayed_terminal_change() -> None:
        await asyncio.sleep(0.06)
        terminal = copy.deepcopy(initial)
        terminal["run"] = {"runId": 1, "status": "completed"}
        await queue.put(SnapshotChange(1, terminal, ()))

    stream = service.stream(
        1,
        1,
        lambda: False,
        is_terminal=lambda: _true(),
        poll_interval=0.01,
    )
    first = await anext(stream)
    assert first.state["run"]["status"] == "running"
    terminal_task = asyncio.create_task(delayed_terminal_change())
    second = await anext(stream)
    assert second.state["run"]["status"] == "completed"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    await terminal_task


@pytest.mark.asyncio
async def test_stream_fallback_sends_terminal_snapshot() -> None:
    """队列通知丢失但 snapshot 已终态时，兜底仍发送完整状态。"""

    initial = empty_snapshot()
    initial["run"] = {"runId": 1, "status": "running"}
    terminal = empty_snapshot()
    terminal["run"] = {"runId": 1, "status": "completed"}
    queue: asyncio.Queue[SnapshotChange] = asyncio.Queue()

    class Snapshots:
        read_count = 0

        def subscribe(self, task_id: int) -> tuple[asyncio.Queue[SnapshotChange], Any]:
            return queue, lambda: None

        def ensure_state_snapshot(self, task_id: int) -> ConversationStateSnapshot:
            self.read_count += 1
            return copy.deepcopy(initial if self.read_count == 1 else terminal)

    service = TransportAssistantService.__new__(TransportAssistantService)
    service._snapshots = Snapshots()
    stream = service.stream(
        1,
        1,
        lambda: False,
        is_terminal=lambda: _true(),
        poll_interval=0.01,
    )
    first = await anext(stream)
    assert first.state["run"]["status"] == "running"
    change = await anext(stream)
    assert change.state["run"]["status"] == "completed"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def _true() -> bool:
    """测试用终态查询。"""

    return True
