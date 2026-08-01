"""Turn cancellation behavior tests."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from fastapi import HTTPException
from langchain_core.messages import AIMessageChunk
from langgraph.graph import END
from pydantic import BaseModel

from app.api.turns_api import cancel_turn as cancel_turn_endpoint
from app.config.settings import Settings
from app.core.runtime.runner import AgentRuntime
from app.core.workflows.react import nodes as react_nodes
from app.core.workflows.react.edges import _after_tools
from app.core.workflows.react.state import ReactGraphState
from app.models.enums.event_type import EventType
from app.models.payload import RunCancelledPayload
from app.models.payload.runtime_event_payload import RuntimeEventPayload
from app.models.runtime_event import RuntimeEvent
from app.models.turn_record import TurnRecord
from app.service.tool_execution.run_result import ToolRunResult
from app.service.tool_execution.tool_execution_service import ToolExecutionService
from app.storage.crud.runtime_event_crud import RuntimeEventCrud
from app.storage.store_engines import close_storage, init_storage
from app.tools.schemas import ToolCall, ToolDefinition, ToolObservation
from app.tools.tool_execute.tool_executor import ToolExecutor


class _NoArgs(BaseModel):
    """Empty pydantic args model for test tool definitions."""


def _sleeping_handler(**_: object) -> str:
    """Sleep long enough for the process cancellation test to interrupt.

    参数:
        **_: Ignored keyword arguments.

    返回:
        A success marker if not cancelled.

    异常:
        无。

    副作用:
        休眠 5 秒。
    """

    time.sleep(5)
    return "finished"


class _FakeTurnService:
    """In-memory turn service for cancellation tests."""

    def __init__(self, turn: TurnRecord) -> None:
        """Store the initial turn record.

        参数:
            turn: 测试使用的初始 turn。

        返回:
            无。

        异常:
            无。

        副作用:
            保存内存状态，供后续断言。
        """

        self.turn = turn
        self.updated_statuses: list[tuple[str, str, str | None]] = []

    def get_turn(self, turn_id: str) -> TurnRecord:
        """Return the configured turn by id.

        参数:
            turn_id: 待查询的 turn 标识。

        返回:
            匹配的 turn。

        异常:
            KeyError: 当 turn_id 与测试 turn 不匹配时抛出。

        副作用:
            无。
        """

        if turn_id != self.turn.turn_id:
            raise KeyError(turn_id)
        return self.turn

    def update_turn_status(
        self, turn_id: str, status: str, end_reason: str | None = None
    ) -> TurnRecord:
        """Update the in-memory turn status.

        参数:
            turn_id: 待更新的 turn 标识。
            status: 新状态。
            end_reason: 可选终态原因。

        返回:
            更新后的 turn。

        异常:
            KeyError: 当 turn_id 与测试 turn 不匹配时抛出。

        副作用:
            记录状态更新历史并替换内存 turn。
        """

        if turn_id != self.turn.turn_id:
            raise KeyError(turn_id)
        self.updated_statuses.append((turn_id, status, end_reason))
        self.turn = replace(self.turn, status=status, end_reason=end_reason)
        return self.turn

    def cancel_turn_if_active(self, turn_id: str, end_reason: str) -> TurnRecord | None:
        """Cancel the turn only when it is pending or running.

        参数:
            turn_id: 待取消的 turn 标识。
            end_reason: 取消原因。

        返回:
            成功取消时返回更新后的 turn；非 active 状态返回 None。

        异常:
            KeyError: 当 turn_id 与测试 turn 不匹配时抛出。

        副作用:
            active 状态时更新内存 turn。
        """

        if self.turn.status not in {"pending", "running"}:
            return None
        return self.update_turn_status(turn_id, "cancelled", end_reason)

    def complete_turn_if_running(self, turn_id: str, response_text: str) -> TurnRecord | None:
        """Complete the turn only when it is still running.

        参数:
            turn_id: 待完成的 turn 标识。
            response_text: Agent 回复文本。

        返回:
            成功完成时返回更新后的 turn；非 running 状态返回 None。

        异常:
            KeyError: 当 turn_id 与测试 turn 不匹配时抛出。

        副作用:
            running 状态时更新内存 turn 和回复文本。
        """

        if turn_id != self.turn.turn_id:
            raise KeyError(turn_id)
        if self.turn.status != "running":
            return None
        self.updated_statuses.append((turn_id, "completed", None))
        self.turn = replace(self.turn, status="completed", response_text=response_text)
        return self.turn

    def fail_turn_if_running(
        self, turn_id: str, end_reason: str | None = None
    ) -> TurnRecord | None:
        """Fail the turn only when it is still running.

        参数:
            turn_id: 待失败落定的 turn 标识。
            end_reason: 可选失败原因。

        返回:
            成功失败落定时返回更新后的 turn；非 running 状态返回 None。

        异常:
            KeyError: 当 turn_id 与测试 turn 不匹配时抛出。

        副作用:
            running 状态时更新内存 turn。
        """

        if turn_id != self.turn.turn_id:
            raise KeyError(turn_id)
        if self.turn.status != "running":
            return None
        return self.update_turn_status(turn_id, "failed", end_reason)


class _RacingTurnService(_FakeTurnService):
    """Fake a completion race between status read and cancellation update."""

    def cancel_turn_if_active(self, turn_id: str, end_reason: str) -> TurnRecord | None:
        """Simulate another writer completing the turn before cancellation.

        参数:
            turn_id: 待取消的 turn 标识。
            end_reason: 取消原因，本 fake 不使用。

        返回:
            None，表示条件更新未命中 active 状态。

        异常:
            KeyError: 当 turn_id 与测试 turn 不匹配时抛出。

        副作用:
            把内存 turn 改为 completed，模拟竞态赢家。
        """

        if turn_id != self.turn.turn_id:
            raise KeyError(turn_id)
        self.turn = replace(self.turn, status="completed")
        return None


class _RecordingRuntimeEventCrud:
    """Capture runtime event saves in memory."""

    saved_events: ClassVar[list[dict[str, Any]]] = []
    next_sequence: ClassVar[int] = 0

    def next_sequence_for_turn(self, turn_id: str) -> int:
        """Return the configured next sequence for a turn.

        参数:
            turn_id: 待查询的 turn 标识。

        返回:
            测试配置的下一事件序号。

        异常:
            无。

        副作用:
            无。
        """

        return self.next_sequence

    def save_event(self, event_dict: dict[str, Any]) -> None:
        """Record the event dictionary.

        参数:
            event_dict: 待保存的运行时事件字典。

        返回:
            无。

        异常:
            无。

        副作用:
            追加到类级列表，供测试断言。
        """

        self.saved_events.append(event_dict)

    def save_event_with_next_sequence(self, event_dict: dict[str, Any]) -> int:
        """Record the event dictionary with the configured next sequence.

        参数:
            event_dict: 待保存的运行时事件字典。

        返回:
            测试配置的下一事件序号。

        异常:
            无。

        副作用:
            追加到类级列表，供测试断言。
        """

        event_dict["sequence"] = self.next_sequence
        self.saved_events.append(event_dict)
        return self.next_sequence


class _FailingRuntimeEventCrud:
    """Runtime event CRUD fake that fails the next-sequence save path."""

    def save_event_with_next_sequence(self, event_dict: dict[str, Any]) -> int:
        """Raise a controlled persistence failure.

        参数:
            event_dict: 待保存的运行时事件字典，本 fake 不使用。

        返回:
            不返回。

        异常:
            RuntimeError: 总是抛出，模拟事件持久化失败。

        副作用:
            无。
        """

        raise RuntimeError("persist failed")


class _RecordingRuntimeEventService:
    """Runtime event service fake backed by _RecordingRuntimeEventCrud."""

    def __init__(self) -> None:
        """Initialize the fake service.

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            创建内存 CRUD fake。
        """

        self._crud = _RecordingRuntimeEventCrud()

    def save_event(self, event: RuntimeEvent) -> RuntimeEvent:
        """Persist an event without publishing.

        参数:
            event: 待记录的 runtime event。

        返回:
            写入 sequence 后的 runtime event。

        异常:
            无。

        副作用:
            追加事件字典到测试记录列表。
        """

        if event.turn_id is None:
            self._crud.save_event(event.to_dict())
            return event
        sequence = self._crud.save_event_with_next_sequence(event.to_dict())
        return replace(event, sequence=sequence)

    def publish_event(self, event: RuntimeEvent) -> None:
        """Ignore live publishing in cancellation unit tests.

        参数:
            event: 待发布的 runtime event，本 fake 不使用。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """

    def save_and_publish(self, event: RuntimeEvent) -> RuntimeEvent:
        """Persist an event and skip live publishing.

        参数:
            event: 待记录的 runtime event。

        返回:
            写入 sequence 后的 runtime event。

        异常:
            无。

        副作用:
            追加事件字典到测试记录列表。
        """

        return self.save_event(event)


class _FakeScheduler:
    """Record tool execution order for cancellation tests."""

    def __init__(self) -> None:
        """Initialize an empty execution log.

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            创建内存列表用于记录工具调用。
        """

        self.executed_tool_names: list[str] = []

    def execute(self, call: ToolCall, **_: object) -> ToolObservation:
        """Record and return a successful tool observation.

        参数:
            call: 待执行的工具调用。
            **_: 测试不关心的执行上下文参数。

        返回:
            成功的 ToolObservation。

        异常:
            无。

        副作用:
            记录工具名称。
        """

        self.executed_tool_names.append(call.tool_name)
        return ToolObservation(
            tool_name=call.tool_name,
            status="success",
            content=f"ran {call.tool_name}",
            tool_call_id=call.call_id,
        )


class _FakeToolOperations:
    """Fake runtime operations for workflow cancellation node tests."""

    def __init__(self) -> None:
        """Initialize the fake operations object.

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            创建调用计数状态。
        """

        self.cancel_checks = 0
        self.tool_call_count = 0

    def is_current_turn_cancelled(self) -> bool:
        """Return False before tool execution and True after it.

        参数:
            无。

        返回:
            工具执行后返回 True。

        异常:
            无。

        副作用:
            推进取消检查计数。
        """

        self.cancel_checks += 1
        return self.tool_call_count > 0

    def run_tool_calls(
        self,
        task_id: str,
        calls: list[ToolCall],
        step_id: str,
        write_event,
    ) -> ToolRunResult:
        """Record that a tool batch ran.

        参数:
            task_id: 当前任务标识。
            calls: 待执行工具调用。
            step_id: 当前步骤标识。
            write_event: 事件写入回调，本 fake 不使用。

        返回:
            一个成功观察结果。

        异常:
            无。

        副作用:
            记录工具批次执行次数。
        """

        self.tool_call_count += 1
        return ToolRunResult(
            observations=[
                ToolObservation(
                    tool_name=calls[0].tool_name,
                    status="success",
                    content="ran tool",
                    tool_call_id=calls[0].call_id,
                )
            ],
            messages_for_model=[],
        )


class _UsageStats:
    """Minimal usage stats fake for model node tests."""

    def add_message_usage(self, usage: dict[str, int | float]) -> None:
        """Ignore per-message usage data.

        参数:
            usage: 模型返回的 usage 数据，本 fake 不使用。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """

    def to_dict(self) -> dict[str, int]:
        """Return zeroed usage counters.

        参数:
            无。

        返回:
            token 计数字典。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
            "reasoning_tokens": 0,
        }


def _turn(status: str) -> TurnRecord:
    """Build a turn record for cancellation tests.

    参数:
        status: 初始 turn 状态。

    返回:
        可用于 runtime 测试的 TurnRecord。

    异常:
        无。

    副作用:
        无。
    """

    now = datetime.now(UTC)
    return TurnRecord(
        turn_id="turn-1",
        task_id="task-1",
        input_text="hello",
        status=status,
        created_at=now,
        updated_at=now,
    )


def _runtime(turn_service: _FakeTurnService) -> AgentRuntime:
    """Build an AgentRuntime with only cancellation collaborators.

    参数:
        turn_service: fake turn service used by cancel_turn.

    返回:
        AgentRuntime 实例。

    异常:
        无。

    副作用:
        无。
    """

    return AgentRuntime(
        task_service=None,  # type: ignore[arg-type]
        turn_service=turn_service,  # type: ignore[arg-type]
        context_builder=None,  # type: ignore[arg-type]
        tool_scheduler=None,  # type: ignore[arg-type]
        agent_registry=None,  # type: ignore[arg-type]
        runtime_event_service=_RecordingRuntimeEventService(),  # type: ignore[arg-type]
    )


def test_cancel_turn_rejects_completed_turn() -> None:
    """Cancelling a completed turn must not rewrite its terminal status."""

    turn_service = _FakeTurnService(_turn("completed"))
    runtime = _runtime(turn_service)

    with pytest.raises(ValueError, match="cannot cancel turn in status completed"):
        runtime.cancel_turn("turn-1")

    assert turn_service.turn.status == "completed"
    assert turn_service.updated_statuses == []


def test_cancel_turn_persists_cancelled_event() -> None:
    """Cancelling a running turn must persist a run_cancelled event."""

    _RecordingRuntimeEventCrud.saved_events = []
    _RecordingRuntimeEventCrud.next_sequence = 0
    turn_service = _FakeTurnService(_turn("running"))
    runtime = _runtime(turn_service)

    updated = runtime.cancel_turn("turn-1")

    assert updated.status == "cancelled"
    assert _RecordingRuntimeEventCrud.saved_events
    assert _RecordingRuntimeEventCrud.saved_events[0]["event_type"] == "run_cancelled"
    assert _RecordingRuntimeEventCrud.saved_events[0]["turn_id"] == "turn-1"


def test_cancel_turn_uses_next_runtime_event_sequence() -> None:
    """Cancelling a running turn must avoid clashing with existing event sequences."""

    _RecordingRuntimeEventCrud.saved_events = []
    _RecordingRuntimeEventCrud.next_sequence = 7
    turn_service = _FakeTurnService(_turn("running"))
    runtime = _runtime(turn_service)

    runtime.cancel_turn("turn-1")

    assert _RecordingRuntimeEventCrud.saved_events[0]["sequence"] == 7


def test_runtime_event_crud_assigns_next_sequence_with_existing_event(
    tmp_path,
) -> None:
    """RuntimeEventCrud must assign the next sequence against real SQLite storage."""

    previous = {
        "DATABASE_FILE": Settings.DATABASE_FILE,
        "LOG_DATABASE_FILE": Settings.LOG_DATABASE_FILE,
        "CHECKPOINT_FILE": Settings.CHECKPOINT_FILE,
    }
    close_storage()
    Settings.override(
        DATABASE_FILE=tmp_path / "app.sqlite3",
        LOG_DATABASE_FILE=tmp_path / "logs.sqlite3",
        CHECKPOINT_FILE=tmp_path / "checkpoint.sqlite3",
    )
    try:
        init_storage()
        crud = RuntimeEventCrud()
        first = RuntimeEvent(
            event_type=EventType.RUN_CANCELLED,
            task_id="task-1",
            turn_id="turn-1",
            payload=RunCancelledPayload(status="cancelled"),
        )
        second = RuntimeEvent(
            event_type=EventType.RUN_CANCELLED,
            task_id="task-1",
            turn_id="turn-1",
            payload=RunCancelledPayload(status="cancelled"),
        )

        first_sequence = crud.save_event_with_next_sequence(first.to_dict())
        second_sequence = crud.save_event_with_next_sequence(second.to_dict())

        assert first_sequence == 0
        assert second_sequence == 1
        assert [event["sequence"] for event in crud.list_by_turn("turn-1")] == [0, 1]
    finally:
        close_storage()
        Settings.override(**previous)


def test_cancel_turn_rejects_completion_race() -> None:
    """A completion race must not be overwritten by cancellation."""

    turn_service = _RacingTurnService(_turn("running"))
    runtime = _runtime(turn_service)

    with pytest.raises(ValueError, match="cannot cancel turn in status completed"):
        runtime.cancel_turn("turn-1")

    assert turn_service.turn.status == "completed"


async def test_cancel_turn_endpoint_maps_terminal_turn_to_conflict() -> None:
    """The cancel endpoint must expose terminal-turn rejection as HTTP 409."""

    turn_service = _FakeTurnService(_turn("completed"))
    runtime = _runtime(turn_service)

    with pytest.raises(HTTPException) as exc_info:
        await cancel_turn_endpoint(
            "turn-1",
            runtime=runtime,
            turn_service=turn_service,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "cannot cancel turn in status completed"


def test_disconnect_cleanup_does_not_overwrite_cancelled_turn() -> None:
    """Disconnect cleanup must not rewrite a cancelled turn to failed."""

    turn_service = _FakeTurnService(_turn("cancelled"))
    runtime = _runtime(turn_service)

    runtime._mark_turn_disconnected_if_running("turn-1")

    assert turn_service.turn.status == "cancelled"
    assert turn_service.updated_statuses == []


def test_tool_batch_stops_before_next_tool_when_cancelled() -> None:
    """A tool batch must stop before executing the next tool after cancellation."""

    scheduler = _FakeScheduler()
    service = ToolExecutionService(
        scheduler=scheduler,  # type: ignore[arg-type]
        agent_id="developer",
        should_cancel=lambda: len(scheduler.executed_tool_names) > 0,
    )
    emitted_events: list[EventType] = []

    def write_event(event_type: EventType, _: RuntimeEventPayload) -> None:
        """Capture emitted tool events.

        参数:
            event_type: 被发出的事件类型。
            _: 测试不检查的 payload。

        返回:
            无。

        异常:
            无。

        副作用:
            记录事件类型。
        """

        emitted_events.append(event_type)

    result = service.run_calls_with_events(
        step_id="step-1",
        calls=[
            ToolCall(tool_name="first", arguments={}, call_id="call-1"),
            ToolCall(tool_name="second", arguments={}, call_id="call-2"),
        ],
        write_event=write_event,
    )

    assert scheduler.executed_tool_names == ["first"]
    assert len(result.observations) == 1
    assert emitted_events.count(EventType.TOOL_CALL_STARTED) == 1


def test_process_tool_is_cancelled_while_running() -> None:
    """A process-mode tool must stop while running when cancellation is requested."""

    tool = ToolDefinition(
        name="sleeping_tool",
        description="sleep",
        permission="execute_terminal",
        handler=_sleeping_handler,
        args_model=_NoArgs,
        timeout_seconds=10,
        execution_mode="process",
    )
    start = time.monotonic()

    observation = ToolExecutor().execute(
        tool,
        {},
        tool_call_id="call-1",
        should_cancel=lambda: time.monotonic() - start > 0.2,
    )

    assert observation.status == "error"
    assert observation.error == "tool execution cancelled"
    assert time.monotonic() - start < 3


async def test_model_node_cancelled_before_request_skips_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled turn must not emit model request events or open the model stream."""

    emitted_events: list[EventType] = []

    class _CancelledOperations:
        """Operations fake that reports cancellation immediately."""

        def is_current_turn_cancelled(self) -> bool:
            """Return True for the current turn.

            参数:
                无。

            返回:
                True。

            异常:
                无。

            副作用:
                无。
            """

            return True

    class _FailingModel:
        """Model fake that must not be called."""

        async def astream(self, messages):
            """Fail if the model stream is opened.

            参数:
                messages: 模型消息，本 fake 不使用。

            返回:
                空异步生成器。

            异常:
                AssertionError: 一旦被调用即抛出。

            副作用:
                无。
            """

            raise AssertionError("model stream should not be opened")
            yield  # pragma: no cover

    monkeypatch.setattr(
        react_nodes,
        "_runtime_config",
        lambda: SimpleNamespace(
            operations=_CancelledOperations(),
            turn=SimpleNamespace(turn_id="turn-1"),
            model=_FailingModel(),
        ),
    )
    monkeypatch.setattr(
        react_nodes,
        "write_event",
        lambda event_type, payload: emitted_events.append(event_type),
    )

    result = await react_nodes._model_node(
        ReactGraphState(
            messages=[],
            step_count=0,
            tool_error_count=0,
            requested_tool=False,
            final_response=False,
            terminal=False,
            pending_tool_calls=[],
            max_steps=3,
            final_text="",
        )
    )

    assert result["terminal"] is True
    assert EventType.MODEL_REQUESTED not in emitted_events


async def test_model_node_cancelled_before_model_requested_skips_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second pre-request cancellation check must stop before MODEL_REQUESTED."""

    emitted_events: list[EventType] = []

    class _CancelsOnSecondCheck:
        """Operations fake that flips to cancelled after STEP_STARTED."""

        def __init__(self) -> None:
            """Initialize cancellation check count.

            参数:
                无。

            返回:
                无。

            异常:
                无。

            副作用:
                创建计数器。
            """

            self.check_count = 0

        def is_current_turn_cancelled(self) -> bool:
            """Return True from the second cancellation check onward.

            参数:
                无。

            返回:
                第二次及后续检查返回 True。

            异常:
                无。

            副作用:
                递增检查计数。
            """

            self.check_count += 1
            return self.check_count >= 2

    class _FailingModel:
        """Model fake that must not be called."""

        async def astream(self, messages):
            """Fail if the model stream is opened.

            参数:
                messages: 模型消息，本 fake 不使用。

            返回:
                空异步生成器。

            异常:
                AssertionError: 一旦被调用即抛出。

            副作用:
                无。
            """

            raise AssertionError("model stream should not be opened")
            yield  # pragma: no cover

    operations = _CancelsOnSecondCheck()
    monkeypatch.setattr(
        react_nodes,
        "_runtime_config",
        lambda: SimpleNamespace(
            operations=operations,
            turn=SimpleNamespace(turn_id="turn-1"),
            model=_FailingModel(),
        ),
    )
    monkeypatch.setattr(
        react_nodes,
        "write_event",
        lambda event_type, payload: emitted_events.append(event_type),
    )

    result = await react_nodes._model_node(
        ReactGraphState(
            messages=[],
            step_count=0,
            tool_error_count=0,
            requested_tool=False,
            final_response=False,
            terminal=False,
            pending_tool_calls=[],
            max_steps=3,
            final_text="",
        )
    )

    assert result["terminal"] is True
    assert EventType.STEP_STARTED in emitted_events
    assert EventType.MODEL_REQUESTED not in emitted_events


async def test_model_node_final_response_does_not_overwrite_cancelled_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled turn must not be overwritten by final response completion."""

    emitted_events: list[EventType] = []

    class _CompletionRaceOperations:
        """Operations fake that loses the completed-vs-cancelled CAS."""

        def is_current_turn_cancelled(self) -> bool:
            """Return False so the model can produce text.

            参数:
                无。

            返回:
                False。

            异常:
                无。

            副作用:
                无。
            """

            return False

        def complete_turn_if_running(self, turn_id: str, response_text: str) -> TurnRecord | None:
            """Report that another terminal state already won.

            参数:
                turn_id: 待完成的 turn 标识，本 fake 不使用。
                response_text: 回复文本，本 fake 不使用。

            返回:
                None，表示 CAS 未命中 running。

            异常:
                无。

            副作用:
                无。
            """

            return None

    class _TextModel:
        """Model fake that yields a final text chunk."""

        async def astream(self, messages):
            """Yield one text chunk.

            参数:
                messages: 模型消息，本 fake 不使用。

            返回:
                异步生成器。

            异常:
                无。

            副作用:
                无。
            """

            yield AIMessageChunk(content="done")

    monkeypatch.setattr(
        react_nodes,
        "_runtime_config",
        lambda: SimpleNamespace(
            operations=_CompletionRaceOperations(),
            turn=SimpleNamespace(turn_id="turn-1"),
            model=_TextModel(),
            usage_stats=_UsageStats(),
            start_time=time.monotonic(),
            langfuse_trace_id=None,
        ),
    )
    monkeypatch.setattr(
        react_nodes,
        "write_event",
        lambda event_type, payload: emitted_events.append(event_type),
    )

    result = await react_nodes._model_node(
        ReactGraphState(
            messages=[],
            step_count=0,
            tool_error_count=0,
            requested_tool=False,
            final_response=False,
            terminal=False,
            pending_tool_calls=[],
            max_steps=3,
            final_text="",
        )
    )

    assert result["terminal"] is True
    assert result["final_response"] is False
    assert EventType.FINAL_RESPONSE not in emitted_events
    assert EventType.RUN_FINISHED not in emitted_events


def test_tools_node_cancelled_after_execution_stops_next_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation after tool execution must end the graph branch."""

    operations = _FakeToolOperations()
    monkeypatch.setattr(
        react_nodes,
        "_runtime_config",
        lambda: SimpleNamespace(
            operations=operations,
            task=SimpleNamespace(task_id="task-1"),
            turn=SimpleNamespace(turn_id="turn-1"),
        ),
    )
    monkeypatch.setattr(react_nodes, "interrupt", lambda payload: payload["tool_calls"])

    result = react_nodes._tools_node(
        ReactGraphState(
            messages=[],
            step_count=1,
            tool_error_count=0,
            requested_tool=True,
            final_response=False,
            terminal=False,
            pending_tool_calls=[{"tool_name": "read_file", "arguments": {}, "call_id": "call-1"}],
            max_steps=3,
            final_text="",
        )
    )

    assert operations.tool_call_count == 1
    assert result["terminal"] is True
    assert result["messages"] == []


def test_after_tools_terminal_state_routes_to_end() -> None:
    """A terminal tools result must not route back into the model node."""

    next_node = _after_tools(
        ReactGraphState(
            messages=[],
            step_count=1,
            tool_error_count=0,
            requested_tool=False,
            final_response=False,
            terminal=True,
            pending_tool_calls=[],
            max_steps=3,
            final_text="",
        )
    )

    assert next_node == END
