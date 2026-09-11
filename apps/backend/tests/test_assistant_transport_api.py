import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app.api.tasks_api import get_task
from app.app import app
from app.assistant_transport.assistant_api import (
    assistant_transport_attach,
    assistant_transport_state,
)
from app.assistant_transport.request import AddMessageCommand, AssistantAttachRequest
from app.assistant_transport.service import conversation_run_command_service as command_module
from app.assistant_transport.service import conversation_task_snapshot_service as snapshot_module
from app.assistant_transport.service import transport_assistant_service as transport_module
from app.assistant_transport.service.conversation_run_command_service import (
    ConversationRunCommandService,
)
from app.assistant_transport.service.conversation_task_snapshot_service import (
    ConversationTaskSnapshotService,
)
from app.assistant_transport.service.transport_assistant_service import TransportAssistantService
from app.assistant_transport.service.transport_stream_service import (
    AssistantTransportStreamService,
)
from app.service.task.conversation_run_service import ConversationRunService


def _snapshot(
    run_id: int | None = None, status: str = "idle", end_reason: str | None = None
) -> dict[str, object]:
    runs = (
        []
        if run_id is None
        else [
            {
                "runId": run_id,
                "status": status,
                "endReason": end_reason,
                "messages": [{"id": f"user-{run_id}", "role": "user", "parts": []}],
                "usage": None,
            }
        ]
    )
    return {
        "runs": runs,
        "current_run_id": run_id,
        "approvals": {},
        "context_usage_ratio": None,
        "context_usage_used": None,
        "context_window_total": None,
        "error": None,
    }


@pytest.mark.parametrize(
    ("run_id", "expected"),
    [(None, "new"), (42, "edit")],
)
def test_assistant_command_batch_classifies_message_modes(
    run_id: int | None,
    expected: str,
) -> None:
    command = AddMessageCommand(
        type="add-message",
        commandId="command-1",
        message={"role": "user", "parts": [{"type": "text", "text": "hello"}]},
    )

    assert TransportAssistantService.classify_run_command(command, run_id) == expected


def test_assistant_command_batch_classifies_empty_resume() -> None:
    assert TransportAssistantService.classify_run_command(None, 42) == "resume"


def test_task_route_has_one_get_method() -> None:
    routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path == "/tasks/{task_id}"
        and route.methods == {"GET"}
    ]

    assert len(routes) == 1
    assert routes[0].methods == {"GET"}


@pytest.mark.asyncio
async def test_task_endpoint_returns_task_response_with_fork_status() -> None:
    now = datetime.now(UTC)
    record = SimpleNamespace(
        id=7,
        workspace_id=3,
        title="task",
        extra=None,
        execution_status=None,
        task_type="user",
        parent_task_id=None,
        parent_run_id=None,
        delegation_id=None,
        context_usage_used=None,
        created_at=now,
        updated_at=now,
    )

    class _TaskService:
        def get_task(self, _task_id: int) -> object:
            return record

        def is_fork_available(self, _task_id: int) -> bool:
            return False

        def get_context_window_total(self, _task_id: int) -> int | None:
            return None

    result = await get_task(7, _TaskService())

    assert result.task_id == 7
    assert result.workspace_id == 3
    assert result.fork_available is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "end_reason", [None, "user_cancelled", "runtime_restarted", "executor_cancelled"]
)
async def test_resume_task_accepts_any_cancelled_end_reason(
    monkeypatch: pytest.MonkeyPatch,
    end_reason: str | None,
) -> None:
    status = "cancelled"
    state = _snapshot(7, status, end_reason)
    latest_run = SimpleNamespace(id=7, status=status, end_reason=end_reason)

    class _TaskService:
        def get_latest_run(self, _task_id: int) -> object:
            return latest_run

    class _SnapshotService:
        def ensure_state_snapshot(self, _task_id: int) -> dict[str, object]:
            return state

    class _RunService:
        def resume_cancelled_run(self, _run_id: int) -> object:
            return SimpleNamespace(id=7, task_id=1, status="running")

    class _CommandCrud:
        def get_by_run(self, _run_id: int) -> None:
            return None

    class _Operation:
        def __enter__(self) -> "_Operation":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class _TaskSpace:
        def operation(self, *, timeout: float) -> _Operation:
            assert timeout == 10
            return _Operation()

    class _TaskSpaces:
        def get_or_create(self, _task_id: int) -> _TaskSpace:
            return _TaskSpace()

    service = ConversationRunCommandService.__new__(ConversationRunCommandService)
    service._task = _TaskService()
    service._snapshots = _SnapshotService()
    service._conversation_run = _RunService()
    service._command = _CommandCrud()
    monkeypatch.setattr(command_module, "task_runtime_spaces", _TaskSpaces())

    result = service.resume_latest_run(task_id=1, run_id=7)

    assert result.initial_state == state
    assert result.execution_mode == "resume"
    assert result.mode == "resume"


@pytest.mark.asyncio
async def test_resume_task_requires_cancelled_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TaskService:
        def get_latest_run(self, _task_id: int) -> object:
            return SimpleNamespace(id=7, status="running", end_reason="executor_cancelled")

    class _TaskSpace:
        def operation(self, *, timeout: float) -> object:
            assert timeout == 10

            class _Operation:
                def __enter__(self) -> "_Operation":
                    return self

                def __exit__(self, *_args: object) -> None:
                    return None

            return _Operation()

    class _TaskSpaces:
        def get_or_create(self, _task_id: int) -> _TaskSpace:
            return _TaskSpace()

    service = ConversationRunCommandService.__new__(ConversationRunCommandService)
    service._task = _TaskService()
    monkeypatch.setattr(command_module, "task_runtime_spaces", _TaskSpaces())
    with pytest.raises(ValueError, match="not resumable"):
        service.resume_latest_run(task_id=1, run_id=7)


def test_attach_route_is_separate_from_business_resume() -> None:
    routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path == "/tasks/{task_id}/assistant/attach"
    ]

    assert len(routes) == 1
    assert routes[0].methods == {"POST"}


@pytest.mark.asyncio
async def test_attach_run_only_subscribes_existing_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _snapshot(7, "running")

    class _RunService:
        def get_run(self, _run_id: int) -> object:
            return SimpleNamespace(id=7, task_id=1, status="running")

    class _SnapshotService:
        async def read(self, _task_id: int) -> dict[str, object]:
            return state

    class _Executor:
        def is_locally_running(self, _run_id: int) -> bool:
            return True

    class _Response:
        def __init__(self, value: object) -> None:
            self.value = value
            self.headers: dict[str, str] = {}

    service = TransportAssistantService.__new__(TransportAssistantService)
    service._runs = _RunService()
    service._snapshots = _SnapshotService()
    service.run_executor = _Executor()
    service._stream = SimpleNamespace(subscribe_run_state=lambda *_args: None)  # type: ignore[attr-defined]
    monkeypatch.setattr(transport_module, "create_run", lambda _callback, state: state)
    monkeypatch.setattr(transport_module, "AssistantTransportResponse", _Response)

    result = await service.attach_run(task_id=1, thread_id="task-1", run_id=7)

    assert result.value == state
    assert result.headers == {"X-Cosir-Task-Id": "1", "X-Cosir-Thread-Id": "task-1"}


@pytest.mark.asyncio
async def test_attach_run_rejects_terminal_run_even_when_snapshot_matches() -> None:
    state = _snapshot(7, "cancelled")

    class _RunService:
        def get_run(self, _run_id: int) -> object:
            return SimpleNamespace(id=7, task_id=1, status="cancelled")

    class _SnapshotService:
        async def read(self, _task_id: int) -> dict[str, object]:
            return state

    service = TransportAssistantService.__new__(TransportAssistantService)
    service._runs = _RunService()
    service._snapshots = _SnapshotService()
    service.run_executor = SimpleNamespace(is_locally_running=lambda _run_id: False)

    with pytest.raises(HTTPException) as error:
        await service.attach_run(task_id=1, thread_id="task-1", run_id=7)

    assert error.value.status_code == 409
    assert error.value.detail["error"]["code"] == "RUN_NOT_ATTACHABLE"


@pytest.mark.asyncio
async def test_attach_endpoint_rejects_business_commands() -> None:
    class _TaskService:
        def get_task(self, _task_id: int) -> object:
            return object()

    with pytest.raises(HTTPException) as error:
        await assistant_transport_attach(
            1,
            AssistantAttachRequest(
                commands=[{"type": "add-message"}],
                taskId=1,
                threadId="task-1",
                runId=7,
            ),
            _TaskService(),
            object(),  # type: ignore[arg-type]
        )

    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_state_read_reconciles_terminal_run_without_starting_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _snapshot(7, "running")
    terminal = _snapshot(7, "cancelled")
    state_reads = 0
    projector_events: list[object] = []

    class _Lock:
        def acquire(self, *, blocking: bool, timeout: float) -> bool:
            assert blocking is True
            assert timeout == 10
            return True

        def release(self) -> None:
            return None

    class _TaskSpace:
        lock = _Lock()

        def __init__(self) -> None:
            self.projection_runs: list[int] = []

        def existing_context_manager(self) -> None:
            return None

        def ensure_context_usage_projection(self, run: object) -> None:
            self.projection_runs.append(run.id)
            raise RuntimeError("projection probe keeps snapshot recovery test isolated")

    class _TaskSpaces:
        def __init__(self) -> None:
            self.space = _TaskSpace()

        def get_or_create(self, _task_id: int) -> _TaskSpace:
            return self.space

    class _RunService:
        def list_runs_for_task(self, _task_id: int) -> list[object]:
            return [
                SimpleNamespace(
                    id=7,
                    status="cancelled",
                    end_reason="runtime_restarted",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            ]

    class _Executor:
        def is_locally_running(self, _run_id: int) -> bool:
            return False

    class _Projector:
        def process(self, event: object) -> None:
            projector_events.append(event)

    service = ConversationTaskSnapshotService.__new__(ConversationTaskSnapshotService)

    def ensure_state_snapshot(_task_id: int) -> dict[str, object]:
        nonlocal state_reads
        state_reads += 1
        return active if state_reads == 1 else terminal

    service.ensure_state_snapshot = ensure_state_snapshot  # type: ignore[method-assign]
    task_spaces = _TaskSpaces()
    monkeypatch.setattr(snapshot_module, "task_runtime_spaces", task_spaces)

    # ``read`` imports dependency getters lazily, so patch the provider module
    # used by that import rather than pretending recovery is a snapshot write.
    import app.service.depends as depends

    monkeypatch.setattr(depends, "get_conversation_run_executor", lambda: _Executor())
    monkeypatch.setattr(depends, "get_conversation_run_service", lambda: _RunService())
    monkeypatch.setattr(depends, "get_conversation_event_projector", lambda: _Projector())

    result = await service.read(1)

    assert next(run for run in result["runs"] if run["runId"] == 7)["status"] == "cancelled"
    assert state_reads == 3
    assert len(projector_events) == 1
    assert task_spaces.space.projection_runs == [7]


def test_restart_recovery_only_updates_run_persistence() -> None:
    calls: list[tuple[int, str]] = []

    class _RunCrud:
        def list_recoverable(self) -> list[object]:
            return [SimpleNamespace(id=7), SimpleNamespace(id=8)]

        def cancel_recoverable_for_restart(
            self, run_id: int, end_reason: str, **_kwargs: object
        ) -> object:
            calls.append((run_id, end_reason))
            return SimpleNamespace(id=run_id)

    service = ConversationRunService.__new__(ConversationRunService)
    service._run = _RunCrud()

    result = service.recover_orphaned_runs()

    assert [record.id for record in result] == [7, 8]
    assert calls == [(7, "runtime_restarted"), (8, "runtime_restarted")]


def test_state_route_has_one_read_method() -> None:
    routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path == "/tasks/{task_id}/assistant/state"
    ]

    assert len(routes) == 1
    assert routes[0].methods == {"GET"}


@pytest.mark.asyncio
async def test_state_endpoint_returns_nested_run_snapshot() -> None:
    snapshot = _snapshot()

    class _TaskService:
        def get_task(self, _task_id: int) -> object:
            return object()

    class _TransportService:
        async def read(self, _task_id: int) -> dict[str, object]:
            return snapshot

    result = await assistant_transport_state(1, _TaskService(), _TransportService())

    assert result == snapshot
    assert set(result) == {
        "runs",
        "current_run_id",
        "approvals",
        "context_usage_ratio",
        "context_usage_used",
        "context_window_total",
        "error",
    }


@pytest.mark.asyncio
async def test_sse_callback_logs_and_returns_on_normal_completion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _noop_stream(*_args, **_kwargs):
        return
        yield  # pragma: no cover

    service = AssistantTransportStreamService.__new__(AssistantTransportStreamService)
    service._runs = SimpleNamespace(get_run=lambda _id: SimpleNamespace(task_id=1))
    service._snapshots = SimpleNamespace(ensure_state_snapshot=lambda _t: _snapshot(7, "completed"))
    service.run_executor = SimpleNamespace(status=lambda *_a: None)
    service.stream = _noop_stream

    with caplog.at_level("INFO"):
        await service.subscribe_run_state(object(), 1, 7)

    events = {record.message for record in caplog.records}
    assert "assistant_sse_callback_started" in events
    assert "assistant_sse_callback_finished" in events


@pytest.mark.asyncio
async def test_sse_callback_logs_and_propagates_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _cancelling_stream(*_args, **_kwargs):
        raise asyncio.CancelledError()
        yield  # pragma: no cover

    service = AssistantTransportStreamService.__new__(AssistantTransportStreamService)
    service._runs = SimpleNamespace(get_run=lambda _id: SimpleNamespace(task_id=1))
    service._snapshots = SimpleNamespace(ensure_state_snapshot=lambda _t: _snapshot(7, "completed"))
    service.run_executor = SimpleNamespace(status=lambda *_a: None)
    service.stream = _cancelling_stream

    with caplog.at_level("WARNING"), pytest.raises(asyncio.CancelledError):
        await service.subscribe_run_state(object(), 1, 12)

    assert "assistant_sse_callback_cancelled" in {record.message for record in caplog.records}


@pytest.mark.asyncio
async def test_sse_callback_logs_and_propagates_failures(caplog: pytest.LogCaptureFixture) -> None:
    async def _failing_stream(*_args, **_kwargs):
        raise RuntimeError("stream failed")
        yield  # pragma: no cover

    service = AssistantTransportStreamService.__new__(AssistantTransportStreamService)
    service._runs = SimpleNamespace(get_run=lambda _id: SimpleNamespace(task_id=1))
    service._snapshots = SimpleNamespace(ensure_state_snapshot=lambda _t: _snapshot(7, "completed"))
    service.run_executor = SimpleNamespace(status=lambda *_a: None)
    service.stream = _failing_stream

    with caplog.at_level("ERROR"), pytest.raises(RuntimeError, match="stream failed"):
        await service.subscribe_run_state(object(), 1, 13)

    assert "assistant_sse_callback_failed" in {record.message for record in caplog.records}


class _Operation:
    """task 运行时空间的独占操作桩。"""

    def __enter__(self) -> "_Operation":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _TaskSpace:
    """task 运行时空间桩。"""

    def operation(self, *, timeout: float) -> _Operation:
        assert timeout == 10
        return _Operation()


class _TaskSpaces:
    """task 运行时空间注册表桩。"""

    def get_or_create(self, _task_id: int) -> _TaskSpace:
        return _TaskSpace()


@pytest.mark.asyncio
async def test_resume_setup_failure_settles_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """写操作之后再次读取快照失败时，必须补偿收敛 run，不能留下无执行器的 active run。"""

    state = _snapshot(7, "cancelled")
    snapshot_reads = 0
    settled: list[tuple[int, str]] = []

    class _TaskService:
        def get_latest_run(self, _task_id: int) -> object:
            return SimpleNamespace(id=7, status="cancelled", end_reason="user_cancelled")

    class _SnapshotService:
        def ensure_state_snapshot(self, _task_id: int) -> dict[str, object]:
            nonlocal snapshot_reads
            snapshot_reads += 1
            if snapshot_reads > 1:
                raise RuntimeError("snapshot read failed")
            return state

    class _RunService:
        def resume_cancelled_run(self, _run_id: int) -> object:
            return SimpleNamespace(id=7, task_id=1, status="running")

        def cancel_run_if_running(
            self,
            run_id: int,
            end_reason: str = "user_cancelled",
            final_output: str | None = None,
            usage_stats: object | None = None,
        ) -> object:
            settled.append((run_id, end_reason))
            return SimpleNamespace(id=run_id, task_id=1, status="cancelled")

    class _CommandCrud:
        def get_by_run(self, _run_id: int) -> object:
            return SimpleNamespace(id=6, command_id="transport-second")

    service = ConversationRunCommandService.__new__(ConversationRunCommandService)
    service._task = _TaskService()
    service._snapshots = _SnapshotService()
    service._conversation_run = _RunService()
    service._command = _CommandCrud()
    monkeypatch.setattr(command_module, "task_runtime_spaces", _TaskSpaces())

    with pytest.raises(RuntimeError, match="snapshot read failed"):
        service.resume_latest_run(task_id=1, run_id=7)

    assert snapshot_reads == 2
    assert settled == [(7, "resume_setup_failed")]


@pytest.mark.asyncio
async def test_resume_reads_command_before_restoring_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """驱动命令读取必须在恢复 run 之前完成：读取失败时 run 仍是 cancelled。"""

    state = _snapshot(7, "cancelled")
    resumed = False

    class _TaskService:
        def get_latest_run(self, _task_id: int) -> object:
            return SimpleNamespace(id=7, status="cancelled", end_reason="user_cancelled")

    class _SnapshotService:
        def ensure_state_snapshot(self, _task_id: int) -> dict[str, object]:
            return state

    class _RunService:
        def resume_cancelled_run(self, _run_id: int) -> object:
            nonlocal resumed
            resumed = True
            return SimpleNamespace(id=7, task_id=1, status="running")

        def cancel_run_if_running(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("读取失败时不应触发收敛")

    class _CommandCrud:
        def get_by_run(self, _run_id: int) -> object:
            raise RuntimeError("command lookup failed")

    service = ConversationRunCommandService.__new__(ConversationRunCommandService)
    service._task = _TaskService()
    service._snapshots = _SnapshotService()
    service._conversation_run = _RunService()
    service._command = _CommandCrud()
    monkeypatch.setattr(command_module, "task_runtime_spaces", _TaskSpaces())

    with pytest.raises(RuntimeError, match="command lookup failed"):
        service.resume_latest_run(task_id=1, run_id=7)

    assert resumed is False


def test_settle_run_start_failure_cancels_active_run() -> None:
    """执行器启动失败后，run 必须被收敛为终态，不能残留 active run。"""

    cancelled: list[tuple[int, str]] = []

    class _RunService:
        def cancel_run_if_running(self, run_id: int, end_reason: str = "user_cancelled") -> object:
            cancelled.append((run_id, end_reason))
            return SimpleNamespace(id=run_id, task_id=1, status="cancelled")

    service = TransportAssistantService.__new__(TransportAssistantService)
    service._runs = _RunService()

    service.settle_run_start_failure(7)

    assert cancelled == [(7, "run_start_failed")]


def test_settle_run_start_failure_swallows_settle_errors() -> None:
    """收敛自身失败只记日志，不能覆盖原始启动异常。"""

    class _RunService:
        def cancel_run_if_running(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("database unavailable")

    service = TransportAssistantService.__new__(TransportAssistantService)
    service._runs = _RunService()

    service.settle_run_start_failure(7)
