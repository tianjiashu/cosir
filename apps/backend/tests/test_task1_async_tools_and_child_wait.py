from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from app.core.observability.tool_trace_recorder import _NullToolTraceRecorder
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.runtime.run_result import ToolRunResult
from app.core.runtime.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.tools.schemas import ToolCall, ToolDefinition, ToolExecutionContext, ToolObservation
from app.core.tools.tool_execute.tool_executor import ToolExecutor
from app.core.tools.tool_handler.child_agent_wait import ChildAgentWaitTool
from app.core.tools.tool_models.child_agent_session_args import ChildAgentWaitResult
from app.core.tools.tool_registry import ToolRegistry
from app.core.workflows.workflow_operations import WorkflowOperations
from app.service.child_agent.async_child_agent_wait_coordinator import (
    AsyncChildAgentWaitCoordinator,
    WaitQueryResult,
)


class _Args(BaseModel):
    value: int = 1


def _context(tmp_path: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        task_id=1,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=10,
    )


def _definition(
    handler: Any, *, handler_kind: str = "sync", parallel_mode: str = "serial"
) -> ToolDefinition:
    return ToolDefinition(
        name="example",
        description="example",
        permission="safe_read",
        handler=handler,
        args_model=_Args,
        handler_kind=handler_kind,
        parallel_mode=parallel_mode,
    )


def test_registry_rejects_sync_async_handler_mismatch() -> None:
    async def async_handler(
        value: int, execution_context: ToolExecutionContext | None = None
    ) -> str:
        return str(value)

    def sync_handler(value: int, execution_context: ToolExecutionContext | None = None) -> str:
        return str(value)

    with pytest.raises(TypeError, match="handler_kind=sync"):
        ToolRegistry([_definition(async_handler)])
    with pytest.raises(TypeError, match="handler_kind=async"):
        ToolRegistry([_definition(sync_handler, handler_kind="async")])
    with pytest.raises(ValueError, match="serial"):
        ToolRegistry([_definition(async_handler, handler_kind="async", parallel_mode="parallel")])
    with pytest.raises(ValueError, match="handler_kind"):
        ToolRegistry([_definition(sync_handler, handler_kind="invalid")])

    registry = ToolRegistry([_definition(async_handler, handler_kind="async")])
    assert registry.get_tool_definition("example").handler_kind == "async"


@pytest.mark.asyncio
async def test_async_executor_runs_handler_on_event_loop_and_reuses_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop_thread = threading.get_ident()
    called_thread: int | None = None

    async def async_handler(
        value: int, execution_context: ToolExecutionContext | None = None
    ) -> ToolObservation:
        nonlocal called_thread
        called_thread = threading.get_ident()
        return ToolObservation(tool_name="example", status="success", content=str(value))

    async def fail_to_thread(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("async tools must not use asyncio.to_thread")

    monkeypatch.setattr(asyncio, "to_thread", fail_to_thread)
    executor = ToolExecutor(ToolRegistry([_definition(async_handler, handler_kind="async")]))

    observation = await executor.execute_async(
        ToolCall(tool_name="example", arguments={"value": 7}, call_id="call-async"),
        execution_context=_context(tmp_path),
    )

    assert observation.status == "success"
    assert observation.content == "7"
    assert observation.tool_call_id == "call-async"
    assert called_thread == loop_thread


@pytest.mark.asyncio
async def test_sync_and_async_executor_entries_reject_the_wrong_handler_kind(
    tmp_path: Path,
) -> None:
    async def async_handler(
        value: int, execution_context: ToolExecutionContext | None = None
    ) -> str:
        return str(value)

    def sync_handler(value: int, execution_context: ToolExecutionContext | None = None) -> str:
        return str(value)

    async_observation = ToolExecutor(
        ToolRegistry([_definition(async_handler, handler_kind="async")])
    ).execute(
        ToolCall(tool_name="example", arguments={"value": 1}, call_id="wrong-sync"),
        execution_context=_context(tmp_path),
    )
    assert async_observation.status == "error"
    assert "execute_async" in async_observation.error

    with pytest.raises(TypeError, match="requires handler_kind=async"):
        await ToolExecutor(ToolRegistry([_definition(sync_handler)])).execute_async(
            ToolCall(tool_name="example", arguments={"value": 1}, call_id="wrong-async"),
            execution_context=_context(tmp_path),
        )


@pytest.mark.asyncio
async def test_workflow_keeps_async_tool_on_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    called_thread: int | None = None

    class FakeExecutor:
        async def execute_async(self, call: ToolCall, **kwargs: Any) -> ToolObservation:
            nonlocal called_thread
            called_thread = threading.get_ident()
            return ToolObservation(
                tool_name=call.tool_name,
                status="success",
                content="async-done",
                tool_call_id=call.call_id,
            )

        def execute(self, *args: Any, **kwargs: Any) -> ToolObservation:
            raise AssertionError("async tool was routed to sync execution")

    async def fail_to_thread(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("async tool was routed through asyncio.to_thread")

    monkeypatch.setattr(asyncio, "to_thread", fail_to_thread)
    definition = _definition(
        lambda value, execution_context=None: value,
        handler_kind="sync",
    )
    definition = ToolDefinition(
        name=definition.name,
        description=definition.description,
        permission=definition.permission,
        handler=definition.handler,
        args_model=definition.args_model,
        handler_kind="async",
    )
    operations = WorkflowOperations.__new__(WorkflowOperations)
    operations._executor = FakeExecutor()
    operations._trace_recorder = _NullToolTraceRecorder()
    operations._execution_context = None
    operations._allowed_tool_names = frozenset({"example"})
    operations._parallel_mode_by_name = {"example": "serial"}
    operations._handler_kind_by_name = {"example": "async"}

    result = await operations.run_tool_calls(
        task_id=1,
        calls=[ToolCall(tool_name="example", arguments={"value": 1}, call_id="async-call")],
    )

    assert isinstance(result, ToolRunResult)
    assert result.observations[0].content == "async-done"
    assert called_thread == threading.get_ident()


@pytest.mark.asyncio
async def test_workflow_routes_unknown_tool_to_sync_executor_path(tmp_path: Path) -> None:
    async def known_async_handler(
        value: int, execution_context: ToolExecutionContext | None = None
    ) -> str:
        return str(value)

    operations = WorkflowOperations.__new__(WorkflowOperations)
    operations._executor = ToolExecutor(
        ToolRegistry([_definition(known_async_handler, handler_kind="async")])
    )
    operations._trace_recorder = _NullToolTraceRecorder()
    operations._execution_context = _context(tmp_path)
    operations._allowed_tool_names = frozenset({"example"})
    operations._parallel_mode_by_name = {"example": "serial"}
    operations._handler_kind_by_name = {"example": "async"}

    result = await operations.run_tool_calls(
        task_id=1,
        calls=[ToolCall(tool_name="missing_tool", arguments={}, call_id="unknown-call")],
    )

    assert len(result.observations) == 1
    observation = result.observations[0]
    assert observation.status == "error"
    assert observation.tool_name == "missing_tool"
    assert observation.error == "unknown tool: missing_tool"


@pytest.mark.asyncio
async def test_tool_call_registry_cancellation_cancels_async_handler_and_cleans_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()
    handler_cancelled = asyncio.Event()
    projected: list[tuple[str, bool]] = []
    run_id = 901001
    call_id = "registry-cancel-call"

    async def async_handler(
        value: int, execution_context: ToolExecutionContext | None = None
    ) -> ToolObservation:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise
        raise AssertionError("handler must not finish normally")

    monkeypatch.setattr(
        "app.core.tools.tool_execute.tool_executor.project_tool_terminal_state",
        lambda **kwargs: projected.append(
            (
                kwargs["observation"].status,
                tool_call_cancellation_registry.is_cancelled(run_id, call_id),
            )
        ),
    )
    executor = ToolExecutor(ToolRegistry([_definition(async_handler, handler_kind="async")]))
    context = ToolExecutionContext(
        task_id=1,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=run_id,
    )
    task = asyncio.create_task(
        executor.execute_async(
            ToolCall(tool_name="example", arguments={"value": 1}, call_id=call_id),
            execution_context=context,
        )
    )

    try:
        await entered.wait()
        tool_call_cancellation_registry.mark_cancelled(run_id, call_id)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)
        assert handler_cancelled.is_set()
        assert projected == [("cancelled", True)]
        assert tool_call_cancellation_registry.is_cancelled(run_id, call_id) is False
    finally:
        tool_call_cancellation_registry.clear(run_id, call_id)


@pytest.mark.asyncio
async def test_workflow_tool_level_async_cancel_returns_observation_and_keeps_run_alive(
    tmp_path: Path,
) -> None:
    """Tool cancellation must settle one async observation, not cancel its parent workflow."""

    entered = asyncio.Event()
    call_id = "workflow-tool-cancel"
    run_id = 901002

    async def async_handler(
        value: int, execution_context: ToolExecutionContext | None = None
    ) -> ToolObservation:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("the cancelled handler must not finish normally")

    operations = WorkflowOperations.__new__(WorkflowOperations)
    operations._executor = ToolExecutor(
        ToolRegistry([_definition(async_handler, handler_kind="async")])
    )
    operations._trace_recorder = _NullToolTraceRecorder()
    operations._execution_context = ToolExecutionContext(
        task_id=1,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=run_id,
    )
    operations._allowed_tool_names = frozenset({"example"})
    operations._parallel_mode_by_name = {"example": "serial"}
    operations._handler_kind_by_name = {"example": "async"}

    async def cancel_tool() -> None:
        await entered.wait()
        tool_call_cancellation_registry.mark_cancelled(run_id, call_id)

    try:
        result = await asyncio.gather(
            operations.run_tool_calls(
                task_id=1,
                calls=[ToolCall(tool_name="example", arguments={"value": 1}, call_id=call_id)],
            ),
            cancel_tool(),
        )
        observations = result[0].observations
        assert len(observations) == 1
        assert observations[0].status == "cancelled"
        assert cancellation_registry.is_cancelled(run_id) is False
    finally:
        tool_call_cancellation_registry.clear(run_id, call_id)


@pytest.mark.asyncio
async def test_wait_registers_before_query_and_recovers_notification_during_query() -> None:
    coordinator = AsyncChildAgentWaitCoordinator(max_waiters=2)
    query_started = asyncio.Event()
    calls = 0

    def query() -> WaitQueryResult:
        nonlocal calls
        calls += 1
        query_started_loop = coordinator.loop
        assert query_started_loop is not None
        query_started_loop.call_soon_threadsafe(query_started.set)
        coordinator.notify(10)
        return WaitQueryResult(ready=calls >= 2, value=calls)

    result = await coordinator.wait_async(10, query, timeout_seconds=1)

    assert query_started.is_set()
    assert result.value == 2
    assert calls == 2
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_one_signal_wakes_multiple_waiters_and_notification_before_await_is_safe() -> None:
    coordinator = AsyncChildAgentWaitCoordinator(max_waiters=3)
    release = threading.Event()
    query_count = 0

    def query() -> WaitQueryResult:
        nonlocal query_count
        query_count += 1
        if release.is_set():
            return WaitQueryResult(ready=True, value="done")
        return WaitQueryResult(ready=False)

    first = asyncio.create_task(coordinator.wait_async(20, query, timeout_seconds=1))
    second = asyncio.create_task(coordinator.wait_async(20, query, timeout_seconds=1))
    await asyncio.sleep(0.05)
    release.set()
    coordinator.notify(20)

    assert (await first).value == "done"
    assert (await second).value == "done"
    assert query_count >= 3
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_wait_timeout_and_interruption_return_structured_outcomes() -> None:
    coordinator = AsyncChildAgentWaitCoordinator(max_waiters=2)

    timeout_result = await coordinator.wait_async(
        30, lambda: WaitQueryResult(ready=False, value="partial"), timeout_seconds=0.01
    )
    assert timeout_result.timed_out is True
    assert timeout_result.value == "partial"
    assert timeout_result.interrupted_by is None

    interrupted = asyncio.create_task(
        coordinator.wait_async(31, lambda: WaitQueryResult(ready=False), timeout_seconds=1)
    )
    await asyncio.sleep(0.02)
    coordinator.interrupt(31, "parent_cancelled")
    interrupted_result = await interrupted
    assert interrupted_result.interrupted_by == "parent_cancelled"
    assert interrupted_result.timed_out is False
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_waiter_slots_are_released_after_cancel_exception_and_shutdown() -> None:
    coordinator = AsyncChildAgentWaitCoordinator(max_waiters=1)

    def never() -> WaitQueryResult:
        return WaitQueryResult(ready=False)

    cancelled = asyncio.create_task(coordinator.wait_async(40, never, timeout_seconds=1))
    await asyncio.sleep(0.02)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    def raising_query() -> WaitQueryResult:
        raise AssertionError("the query must be synchronous")

    with pytest.raises(AssertionError):
        await coordinator.wait_async(41, raising_query, timeout_seconds=1)

    accepted = asyncio.create_task(coordinator.wait_async(42, never, timeout_seconds=1))
    await asyncio.sleep(0.02)
    await coordinator.shutdown()
    assert (await accepted).interrupted_by == "shutdown"


@pytest.mark.asyncio
async def test_child_agent_wait_capacity_exhaustion_is_a_stable_tool_error(tmp_path: Path) -> None:
    coordinator = AsyncChildAgentWaitCoordinator(max_waiters=1)
    tool = ChildAgentWaitTool(
        lambda **kwargs: WaitQueryResult(ready=False),
        coordinator,
    )
    first = asyncio.create_task(
        tool.execute_async(
            targets=[{"child_task_id": 1}],
            execution_context=_context(tmp_path),
            timeout_seconds=1,
        )
    )
    await asyncio.sleep(0.02)

    second = await tool.execute_async(
        targets=[{"child_task_id": 2}],
        execution_context=_context(tmp_path),
        timeout_seconds=1,
    )

    assert second.status == "error"
    assert second.error == "child_agent_wait_concurrency_exceeded"
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_child_agent_wait_returns_canonical_records_without_loop_db_read(
    tmp_path: Path,
) -> None:
    loop_thread = threading.get_ident()
    read_thread: int | None = None

    def read_wait(**kwargs: Any) -> WaitQueryResult:
        nonlocal read_thread
        read_thread = threading.get_ident()
        return WaitQueryResult(
            ready=True,
            value={
                "messages": [
                    {
                        "child_task_id": 7,
                        "child_run_id": 8,
                        "status": "completed",
                        "final_output": "done",
                        "end_reason": None,
                    }
                ],
                "pending": [],
            },
        )

    coordinator = AsyncChildAgentWaitCoordinator(max_waiters=2)
    tool = ChildAgentWaitTool(read_wait, coordinator)
    observation = await tool.execute_async(
        targets=[{"child_task_id": 7}],
        wait_mode="any",
        timeout_seconds=1,
        execution_context=_context(tmp_path),
    )

    payload = json.loads(observation.content)
    assert observation.status == "success"
    assert payload == {
        "timed_out": False,
        "messages": [
            {
                "child_task_id": 7,
                "child_run_id": 8,
                "status": "completed",
                "final_output": "done",
                "end_reason": None,
            }
        ],
        "pending": [],
        "interrupted_by": None,
    }
    assert read_thread is not None
    assert read_thread != loop_thread
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_wait_reader_accepts_canonical_pydantic_result_and_cleans_signals(
    tmp_path: Path,
) -> None:
    coordinator = AsyncChildAgentWaitCoordinator(max_waiters=1)
    canonical = ChildAgentWaitResult(
        timed_out=False,
        messages=[
            {
                "child_task_id": 3,
                "child_run_id": 4,
                "status": "completed",
                "final_output": "pydantic-result",
            }
        ],
    )
    tool = ChildAgentWaitTool(lambda **kwargs: canonical, coordinator)

    observation = await tool.execute_async(execution_context=_context(tmp_path))

    assert observation.status == "success"
    assert json.loads(observation.content) == {
        "timed_out": False,
        "messages": [
            {
                "child_task_id": 3,
                "child_run_id": 4,
                "status": "completed",
                "final_output": "pydantic-result",
                "end_reason": None,
            }
        ],
        "pending": [],
        "interrupted_by": None,
    }
    assert coordinator._signals == {}
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_wait_reader_accepts_mapping_and_rejects_mismatched_result(
    tmp_path: Path,
) -> None:
    coordinator = AsyncChildAgentWaitCoordinator(max_waiters=2)
    mapping_tool = ChildAgentWaitTool(
        lambda **kwargs: {
            "timed_out": False,
            "messages": [],
            "pending": [],
            "interrupted_by": None,
        },
        coordinator,
    )
    mapping_observation = await mapping_tool.execute_async(execution_context=_context(tmp_path))
    assert mapping_observation.status == "success"
    assert json.loads(mapping_observation.content)["messages"] == []

    invalid_tool = ChildAgentWaitTool(lambda **kwargs: "not-a-wait-result", coordinator)
    invalid_observation = await invalid_tool.execute_async(execution_context=_context(tmp_path))
    assert invalid_observation.status == "error"
    assert "ChildAgentWaitResult" in invalid_observation.error
    assert coordinator._signals == {}
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_async_cancellation_projects_cancelled_terminal_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    projected: list[str] = []

    async def async_handler(
        value: int, execution_context: ToolExecutionContext | None = None
    ) -> ToolObservation:
        entered.set()
        await release.wait()
        return ToolObservation(tool_name="example", status="success", content=str(value))

    def record_projection(**kwargs: Any) -> bool:
        projected.append(kwargs["observation"].status)
        return True

    monkeypatch.setattr(
        "app.core.tools.tool_execute.tool_executor.project_tool_terminal_state",
        record_projection,
    )
    executor = ToolExecutor(ToolRegistry([_definition(async_handler, handler_kind="async")]))
    task = asyncio.create_task(
        executor.execute_async(
            ToolCall(tool_name="example", arguments={"value": 1}, call_id="cancel-async"),
            execution_context=_context(tmp_path),
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert projected == ["cancelled"]


@pytest.mark.asyncio
async def test_coordinator_does_not_create_signals_for_notifications_without_waiters() -> None:
    coordinator = AsyncChildAgentWaitCoordinator(max_waiters=1)
    coordinator.notify(999)
    coordinator.interrupt(999, "parent_cancelled")
    assert coordinator._signals == {}
    await coordinator.shutdown()
