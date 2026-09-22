"""Task 2 final-acceptance regressions for async boundaries and finalization failures."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from types import SimpleNamespace
from typing import Any

import pytest

import app.assistant_transport.service.conversation_run_executor as executor_module
import app.lifespan as lifespan_module
import app.service.child_agent.child_agent_session_service as child_session_module
from app.assistant_transport.service.conversation_run_executor import ConversationRunExecutor
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.service.child_agent.child_agent_session_service import ChildAgentSessionService
from app.service.task.conversation_run_state_service import ConversationRunStateService

_RUN_ID = 7001


class _ThreadCheckedRunService:
    def __init__(self, *, status: str = ConversationRunStatus.RUNNING.value) -> None:
        self.run = SimpleNamespace(id=_RUN_ID, task_id=7, status=status)
        self.owner_thread = threading.get_ident()
        self.get_calls = 0

    def get_run(self, _run_id: int) -> SimpleNamespace:
        self.get_calls += 1
        if threading.get_ident() == self.owner_thread:
            raise AssertionError("canonical get_run ran on the event-loop thread")
        return self.run

    def cancel_run_if_running(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def fail_run_if_running(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _ChildSessions:
    def close_children(self, _run_id: int) -> None:
        return None

    def sweep_pending_follow_ups(self) -> int:
        return 0

    def cancel_descendants(self, _run_id: int) -> None:
        return None

    def shutdown(self) -> None:
        return None


def _executor(run_service: object) -> ConversationRunExecutor:
    executor = ConversationRunExecutor.__new__(ConversationRunExecutor)
    executor._run_service = run_service
    executor._event_projector = None
    executor._child_sessions = _ChildSessions()
    executor._signal = cancellation_registry
    executor._executions = {}
    return executor


@pytest.fixture(autouse=True)
def _clear_run_signal() -> None:
    cancellation_registry.clear(_RUN_ID)
    yield
    cancellation_registry.clear(_RUN_ID)


@pytest.mark.asyncio
async def test_executor_start_reads_canonical_run_off_event_loop() -> None:
    service = _ThreadCheckedRunService()
    executor = _executor(service)

    async def runner(_run: object) -> None:
        return None

    task = await executor.start(_RUN_ID, runner)
    await task


@pytest.mark.asyncio
async def test_executor_execute_reads_canonical_run_off_event_loop() -> None:
    service = _ThreadCheckedRunService()
    executor = _executor(service)

    await executor._execute(_RUN_ID, lambda _run: asyncio.sleep(0))


@pytest.mark.asyncio
async def test_executor_tool_settlement_reads_canonical_run_off_event_loop() -> None:
    service = _ThreadCheckedRunService()
    executor = _executor(service)
    executor._event_projector = SimpleNamespace(process=lambda _event: None)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        executor_module,
        "get_terminal_session_service",
        lambda: SimpleNamespace(
            begin_run=lambda _run_id: None,
            close_run_terminals=lambda *_args, **_kwargs: None,
        ),
    )

    async def failing_runner(_run: object) -> None:
        raise RuntimeError("runner failed")

    try:
        with pytest.raises(RuntimeError, match="runner failed"):
            await executor._execute(_RUN_ID, failing_runner)
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_executor_cancel_reads_canonical_run_off_event_loop() -> None:
    service = _ThreadCheckedRunService()
    executor = _executor(service)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(executor_module, "get_terminal_session_service", lambda: SimpleNamespace(
        close_run_terminals=lambda *_args, **_kwargs: None
    ))
    try:
        assert await executor.cancel(_RUN_ID) is True
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_executor_cancel_tool_call_reads_canonical_run_off_event_loop() -> None:
    service = _ThreadCheckedRunService()
    executor = _executor(service)

    assert await executor.cancel_tool_call(_RUN_ID, "tool-1") is True


def test_state_finalization_notifies_with_conditional_write_record_without_sync_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = SimpleNamespace(id=_RUN_ID, task_id=7, status=ConversationRunStatus.COMPLETED.value)

    class _Crud:
        def update_status_if_in(self, *_args: Any, **_kwargs: Any) -> SimpleNamespace:
            return record

        def get(self, _run_id: int) -> SimpleNamespace:
            raise AssertionError("finalization must not perform a synchronous canonical read")

    service = ConversationRunStateService.__new__(ConversationRunStateService)
    service._run = _Crud()
    service._finalization_observer = SimpleNamespace(
        on_run_finalized=lambda value: observed.append(value)
    )
    observed: list[object] = []
    monkeypatch.setattr(
        "app.service.task.conversation_run_state_service.dispatch_conversation_event",
        lambda _event: None,
    )

    finalized = service.complete_run_if_running(_RUN_ID)

    assert finalized is record
    assert observed == [record]


@pytest.mark.asyncio
async def test_startup_failure_dependency_cleanup_runs_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_thread = threading.get_ident()
    calls: list[str] = []

    class _Executor:
        async def close(self) -> None:
            calls.append("executor_close")

    class _Terminal:
        def shutdown(self) -> None:
            calls.append("terminal_shutdown")

    class _Getter:
        def __init__(self, value: object) -> None:
            self.value = value

        def cache_info(self) -> SimpleNamespace:
            return SimpleNamespace(currsize=1)

        def __call__(self) -> object:
            return self.value

    def close_dependencies() -> None:
        assert threading.get_ident() != owner_thread
        calls.append("close_dependencies")

    monkeypatch.setattr(lifespan_module, "get_conversation_run_executor", _Getter(_Executor()))
    monkeypatch.setattr(lifespan_module, "get_child_agent_session_service", _Getter(object()))
    monkeypatch.setattr(lifespan_module, "get_terminal_session_service", _Getter(_Terminal()))
    monkeypatch.setattr(lifespan_module, "close_service_dependencies", close_dependencies)

    await lifespan_module._cleanup_startup_failure()

    assert calls == ["executor_close", "terminal_shutdown", "close_dependencies"]


@pytest.mark.asyncio
async def test_normal_shutdown_dependency_cleanup_runs_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_thread = threading.get_ident()
    calls: list[str] = []

    class _RunService:
        def recover_orphaned_runs(self) -> list[object]:
            return []

        def list_latest_runs(self) -> list[object]:
            return []

    class _Terminal:
        def initialize(self) -> None:
            return None

        def shutdown(self) -> None:
            calls.append("terminal_shutdown")

    class _Executor:
        async def close(self) -> None:
            calls.append("executor_close")

    class _ToolSystem:
        @classmethod
        def build_tool_system(cls) -> object:
            return object()

    class _RuntimeFactory:
        def __init__(self) -> None:
            return None

    monkeypatch.setattr(
        lifespan_module, "install_logging_for_current_process", lambda **_kwargs: None
    )
    monkeypatch.setattr(lifespan_module, "shutdown_logging", lambda: None)
    monkeypatch.setattr(lifespan_module.Settings, "load", staticmethod(lambda: None))
    monkeypatch.setattr(lifespan_module, "initialize_service_dependencies", lambda: None)
    monkeypatch.setattr(lifespan_module, "_ensure_system_cosir_dir", lambda: None)
    monkeypatch.setattr(lifespan_module, "get_conversation_run_service", lambda: _RunService())
    monkeypatch.setattr(lifespan_module, "get_task_service", lambda: object())
    monkeypatch.setattr(lifespan_module, "get_conversation_run_state_service", lambda: object())
    monkeypatch.setattr(lifespan_module, "get_terminal_session_service", lambda: _Terminal())
    monkeypatch.setattr(lifespan_module, "get_conversation_run_executor", lambda: _Executor())
    monkeypatch.setattr(lifespan_module, "get_delegation_service", lambda: SimpleNamespace(
        mark_interrupted_delegations_failed=lambda _reason: None
    ))
    monkeypatch.setattr(lifespan_module, "get_child_agent_session_service", lambda: object())
    monkeypatch.setattr(lifespan_module, "ToolSystem", _ToolSystem)
    monkeypatch.setattr(lifespan_module, "ToolRuntimeOutputChannelFactory", _RuntimeFactory)
    monkeypatch.setattr(lifespan_module, "build_agent_registry", lambda: object())
    monkeypatch.setattr(lifespan_module, "set_tool_system", lambda _value: None)
    monkeypatch.setattr(lifespan_module, "set_agent_registry", lambda _value: None)
    monkeypatch.setattr(lifespan_module, "set_runtime", lambda _value: None)
    monkeypatch.setattr(lifespan_module, "AgentRuntime", lambda **_kwargs: object())
    monkeypatch.setattr(lifespan_module, "flush_langfuse", lambda: None)
    monkeypatch.setattr(lifespan_module.HookInterceptor, "safe_fire", lambda *_args: None)
    monkeypatch.setattr("app.core.hook.initialize_hook_registry", lambda: None)

    def close_dependencies() -> None:
        assert threading.get_ident() != owner_thread
        calls.append("close_dependencies")

    monkeypatch.setattr(lifespan_module, "close_service_dependencies", close_dependencies)

    async with lifespan_module._lifespan_impl(SimpleNamespace()):
        pass

    assert calls[-2:] == ["terminal_shutdown", "close_dependencies"]


def test_finalization_worker_future_logs_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ChildAgentSessionService.__new__(ChildAgentSessionService)
    service._lock = threading.RLock()
    service._shutdown = False
    future: Future[object] = Future()
    submitted: list[tuple[Any, tuple[Any, ...]]] = []

    class _Workers:
        def submit(self, callback: Any, *args: Any) -> Future[object]:
            submitted.append((callback, args))
            return future

    service._finalization_workers = _Workers()
    errors: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        child_session_module.log,
        "error",
        lambda event, *, extra: errors.append((event, extra["data"])),
    )

    service.on_run_finalized(SimpleNamespace(id=_RUN_ID, task_id=7))
    future.set_exception(RuntimeError("child finalization exploded"))

    assert submitted
    assert errors[-1][0] == "child_agent_finalization_worker_failed"
    assert errors[-1][1]["run_id"] == _RUN_ID


@pytest.mark.asyncio
async def test_child_finalization_wait_failure_does_not_skip_follow_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_task2_child_agent_sessions import _service

    service, tasks, runs, executor = await _service()
    started = await asyncio.to_thread(
        service.start_child,
        parent_task_id=1,
        parent_run_id=2,
        workspace_id=3,
        child_agent_id="child",
        title="review",
        prompt="inspect",
        tool_call_id="wait-follow-up-isolation",
        run_callback=lambda _run: asyncio.sleep(0),
        runtime_event_loop=asyncio.get_running_loop(),
    )
    session = service._by_child_task[started.child_task_id]
    service.send(
        parent_task_id=1,
        parent_run_id=2,
        child_task_id=started.child_task_id,
        message="follow up",
        message_id="wait-follow-up-message",
    )
    runs.runs[started.child_run_id].status = ConversationRunStatus.COMPLETED.value
    scheduled: list[int] = []
    monkeypatch.setattr(service, "notify_waiters", lambda _parent_run_id: (_ for _ in ()).throw(
        RuntimeError("wait notification failed")
    ))
    monkeypatch.setattr(
        service, "_schedule_follow_up", lambda value: scheduled.append(value.child_task_id)
    )

    service.notify_child_finalized(runs.runs[started.child_run_id])

    assert scheduled == [started.child_task_id]
    assert session.follow_up_pending is True
    await executor.tasks[started.child_run_id]
    del tasks
