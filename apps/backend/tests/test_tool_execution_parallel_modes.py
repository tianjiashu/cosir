"""Verify serial and parallel scheduling for model-requested tool batches."""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from unittest.mock import MagicMock

from pydantic import BaseModel

from app.config.settings import Settings
from app.service.tool_execution.tool_execution_service import ToolExecutionService
from app.tools.schemas import ToolCall, ToolDefinition, ToolObservation
from app.tools.tool_execute.tool_scheduler import ToolScheduler


class EmptyArgs(BaseModel):
    """Minimal tool argument schema for test-only tool definitions."""


@dataclass(frozen=True)
class CallWindow:
    """Recorded execution window for one tool call."""

    start: float
    end: float
    thread_id: int


class RecordingScheduler:
    """Scheduler fake that records call timing while returning successful observations."""

    def __init__(self, sleep_seconds: float = 0.08) -> None:
        """Initialize the recording fake.

        Args:
            sleep_seconds: Artificial duration for every tool call.

        Returns:
            None.

        Raises:
            None.

        Side Effects:
            Creates an in-memory timing map guarded by a thread lock.
        """
        self._sleep_seconds = sleep_seconds
        self._lock = threading.Lock()
        self.windows: dict[str, CallWindow] = {}

    def execute(self, call: ToolCall, **_kwargs: object) -> ToolObservation:
        """Record an execution window and return a successful observation.

        Args:
            call: Tool call being executed.
            **_kwargs: Scheduler options passed by the service.

        Returns:
            A successful ``ToolObservation`` bound to the input call.

        Raises:
            None.

        Side Effects:
            Sleeps for the configured duration and records timing/thread metadata.
        """
        start = time.monotonic()
        time.sleep(self._sleep_seconds)
        end = time.monotonic()
        with self._lock:
            self.windows[call.call_id] = CallWindow(
                start=start,
                end=end,
                thread_id=threading.get_ident(),
            )
        return ToolObservation(
            tool_name=call.tool_name,
            status="success",
            content=f"ok:{call.call_id}",
            error="",
            reason="",
            retryable=False,
            tool_call_id=call.call_id,
        )


def _make_call(call_id: str, tool_name: str) -> ToolCall:
    """Create a minimal model tool call.

    Args:
        call_id: Stable tool call identifier.
        tool_name: Tool name used for scheduling lookup.

    Returns:
        A ``ToolCall`` with empty arguments.

    Raises:
        None.

    Side Effects:
        None.
    """
    return ToolCall(call_id=call_id, tool_name=tool_name, arguments={})


def _definition(name: str, parallel_mode: str = "serial") -> ToolDefinition:
    """Create a test tool definition with the requested scheduling mode.

    Args:
        name: Tool name.
        parallel_mode: Scheduling mode under test.

    Returns:
        A normalized-compatible ``ToolDefinition``.

    Raises:
        None.

    Side Effects:
        None.
    """
    return ToolDefinition(
        name=name,
        description=f"{name} test tool",
        permission="read",
        handler=lambda **_kwargs: None,
        args_model=EmptyArgs,
        parallel_mode=parallel_mode,  # type: ignore[arg-type]
    )


def _make_service(
    scheduler: ToolScheduler,
    tool_definitions: list[ToolDefinition] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ToolExecutionService:
    """Build a ``ToolExecutionService`` for scheduling tests.

    Args:
        scheduler: Scheduler fake or mock.
        tool_definitions: Tool definitions exposed to the run.
        should_cancel: Optional cancellation callback.

    Returns:
        Configured ``ToolExecutionService``.

    Raises:
        None.

    Side Effects:
        None.
    """
    return ToolExecutionService(
        scheduler=scheduler,
        agent_id="test-agent",
        tool_definitions=tool_definitions,
        should_cancel=should_cancel,
        trace_recorder=MagicMock(),
    )


def test_parallel_group_runs_after_serial_group_and_keeps_model_order() -> None:
    """Mixed batch: serial calls run first in order, then the parallel group overlaps."""
    scheduler = RecordingScheduler()
    calls = [
        _make_call("s1", "serial_tool"),
        _make_call("p1", "parallel_tool"),
        _make_call("p2", "parallel_tool"),
        _make_call("s2", "serial_tool"),
    ]
    service = _make_service(
        scheduler,  # type: ignore[arg-type]
        tool_definitions=[
            _definition("serial_tool", "serial"),
            _definition("parallel_tool", "parallel"),
        ],
        should_cancel=lambda: False,
    )

    result = service.run_calls_with_events(
        step_id="step-1",
        calls=calls,
        write_event=MagicMock(),
    )

    windows = scheduler.windows
    # 串行组（s1/s2）先按原始相对顺序逐个执行，随后并行组（p1/p2）统一并发
    assert windows["s1"].end <= windows["s2"].start
    assert windows["s2"].end <= min(windows["p1"].start, windows["p2"].start)
    assert windows["p1"].start < windows["p2"].end
    assert windows["p2"].start < windows["p1"].end
    assert windows["p1"].thread_id != windows["p2"].thread_id
    assert [o.tool_call_id for o in result.observations] == ["s1", "p1", "p2", "s2"]
    assert [m.metadata["tool_call_id"] for m in result.messages_for_model] == [
        "s1",
        "p1",
        "p2",
        "s2",
    ]


def test_missing_tool_definition_defaults_to_serial_execution() -> None:
    """Unknown tool scheduling metadata should fall back to serial execution."""
    scheduler = RecordingScheduler()
    calls = [
        _make_call("c1", "unknown_tool"),
        _make_call("c2", "unknown_tool"),
    ]
    service = _make_service(
        scheduler,  # type: ignore[arg-type]
        tool_definitions=[],
        should_cancel=lambda: False,
    )

    result = service.run_calls_with_events(
        step_id="step-1",
        calls=calls,
        write_event=MagicMock(),
    )

    assert scheduler.windows["c1"].end <= scheduler.windows["c2"].start
    assert [o.tool_call_id for o in result.observations] == ["c1", "c2"]


def test_cancellation_before_parallel_batch_fills_placeholders_without_running_calls() -> None:
    """A cancellation signal before a parallel batch should skip that whole batch."""
    scheduler = RecordingScheduler()
    calls = [
        _make_call("p1", "parallel_tool"),
        _make_call("p2", "parallel_tool"),
    ]
    service = _make_service(
        scheduler,  # type: ignore[arg-type]
        tool_definitions=[_definition("parallel_tool", "parallel")],
        should_cancel=lambda: True,
    )

    result = service.run_calls_with_events(
        step_id="step-1",
        calls=calls,
        write_event=MagicMock(),
    )

    assert scheduler.windows == {}
    assert [o.tool_call_id for o in result.observations] == ["p1", "p2"]
    assert all(o.status == "cancelled" for o in result.observations)


def test_parallel_batch_does_not_start_queued_calls_after_cancellation() -> None:
    """Queued parallel calls should remain unstarted when cancellation fires."""
    scheduler = RecordingScheduler(sleep_seconds=0.01)
    calls = [
        _make_call("p1", "parallel_tool"),
        _make_call("p2", "parallel_tool"),
        _make_call("p3", "parallel_tool"),
    ]
    state = {"cancelled": False}

    original_limit = Settings.MAX_PARALLEL_TOOL_CALLS
    Settings.override(MAX_PARALLEL_TOOL_CALLS=1)
    try:
        original_execute = scheduler.execute

        def _execute_and_cancel_after_first(call: ToolCall, **kwargs: object) -> ToolObservation:
            observation = original_execute(call, **kwargs)
            state["cancelled"] = True
            return observation

        scheduler.execute = _execute_and_cancel_after_first  # type: ignore[method-assign]
        service = _make_service(
            scheduler,  # type: ignore[arg-type]
            tool_definitions=[_definition("parallel_tool", "parallel")],
            should_cancel=lambda: state["cancelled"],
        )

        result = service.run_calls_with_events(
            step_id="step-1",
            calls=calls,
            write_event=MagicMock(),
        )
    finally:
        Settings.override(MAX_PARALLEL_TOOL_CALLS=original_limit)

    # 取消生效后只应启动一个调用（并发上限为 1），未启动的排队调用补 cancelled 占位。
    # 不绑定具体哪个 call 先启动——并行批的启动顺序由线程池提交次序决定，非契约。
    assert len(scheduler.windows) == 1
    assert [o.tool_call_id for o in result.observations] == ["p1", "p2", "p3"]
    assert sorted(o.status for o in result.observations) == ["cancelled", "cancelled", "success"]
