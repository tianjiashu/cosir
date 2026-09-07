import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app.api.tasks_api import get_task
from app.app import app
from app.assistant_transport.assistant_api import assistant_transport_state
from app.assistant_transport.service import transport_assistant_service as transport_module
from app.assistant_transport.service.transport_assistant_service import TransportAssistantService


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

    result = await get_task(7, _TaskService())

    assert result.task_id == 7
    assert result.workspace_id == 3
    assert result.fork_available is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "end_reason", "expected_mode"),
    [
        ("pending", None, "fresh"),
        ("running", None, "resume"),
        ("cancelled", "user_cancelled", "resume"),
    ],
)
async def test_resume_task_starts_missing_executor_with_correct_mode(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    end_reason: str | None,
    expected_mode: str,
) -> None:
    state = {
        "messages": [],
        "run": {"runId": 7, "status": status},
        "approvals": {},
        "context_usage": 0.0,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
            "reasoning_tokens": 0,
        },
        "error": None,
    }
    latest_run = SimpleNamespace(id=7, status=status, end_reason=end_reason)
    started_modes: list[str] = []

    class _TaskService:
        def get_task(self, _task_id: int) -> object:
            return object()

        def get_latest_run(self, _task_id: int) -> object:
            return latest_run

    class _SnapshotService:
        async def read(self, _task_id: int) -> dict[str, object]:
            return state

    class _RunExecutor:
        def is_cancelling(self, _run_id: int) -> bool:
            return False

        def is_locally_running(self, _run_id: int) -> bool:
            return False

    service = TransportAssistantService.__new__(TransportAssistantService)
    service.task_service = _TaskService()
    service._snapshots = _SnapshotService()
    service.run_executor = _RunExecutor()

    async def start_run(_run_id: int, mode: str) -> None:
        started_modes.append(mode)

    class _Response:
        def __init__(self, value: object) -> None:
            self.value = value
            self.headers: dict[str, str] = {}

    service.start_run = start_run  # type: ignore[method-assign]
    monkeypatch.setattr(transport_module, "create_run", lambda _callback, state: state)
    monkeypatch.setattr(transport_module, "AssistantTransportResponse", _Response)

    result = await service.resume_run(task_id=1, thread_id="task-1", run_id=7)

    assert result.value == state
    assert started_modes == [expected_mode]


@pytest.mark.asyncio
async def test_resume_task_rejects_non_user_cancelled_run() -> None:
    class _TaskService:
        def get_task(self, _task_id: int) -> object:
            return object()

        def get_latest_run(self, _task_id: int) -> object:
            return SimpleNamespace(id=7, status="cancelled", end_reason="executor_cancelled")

    class _SnapshotService:
        async def read(self, _task_id: int) -> dict[str, object]:
            return {"run": {"runId": 7, "status": "cancelled"}}

    service = TransportAssistantService.__new__(TransportAssistantService)
    service.task_service = _TaskService()
    service._snapshots = _SnapshotService()

    with pytest.raises(HTTPException) as error:
        await service.resume_run(task_id=1, thread_id="task-1", run_id=7)

    assert error.value.status_code == 409


def test_state_route_has_one_read_method() -> None:
    routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path == "/tasks/{task_id}/assistant/state"
    ]

    assert len(routes) == 1
    assert routes[0].methods == {"GET"}


@pytest.mark.asyncio
async def test_state_endpoint_returns_snapshot_fields_at_top_level() -> None:
    snapshot = {
        "messages": [],
        "run": {"runId": None, "status": "idle"},
        "approvals": {},
        "context_usage": 0.0,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
            "reasoning_tokens": 0,
        },
        "error": None,
    }

    class _TaskService:
        def get_task(self, _task_id: int) -> object:
            return object()

    class _TransportService:
        async def read(self, _task_id: int) -> dict[str, object]:
            return snapshot

    result = await assistant_transport_state(1, _TaskService(), _TransportService())

    assert result == snapshot
    assert set(result) == {
        "messages",
        "run",
        "approvals",
        "context_usage",
        "usage",
        "error",
    }


@pytest.mark.asyncio
async def test_sse_callback_logs_and_returns_on_normal_completion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def subscribe(_controller: object, _task_id: int, _run_id: int) -> None:
        return None

    with caplog.at_level("INFO"):
        await TransportAssistantService.subscribe_run_state_with_logging(
            SimpleNamespace(subscribe_run_state=subscribe),
            object(),
            7,
            11,
        )

    events = {record.message for record in caplog.records}
    assert "assistant_sse_callback_started" in events
    assert "assistant_sse_callback_finished" in events


@pytest.mark.asyncio
async def test_sse_callback_logs_and_propagates_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def subscribe(_controller: object, _task_id: int, _run_id: int) -> None:
        raise asyncio.CancelledError()

    with caplog.at_level("WARNING"), pytest.raises(asyncio.CancelledError):
        await TransportAssistantService.subscribe_run_state_with_logging(
            SimpleNamespace(subscribe_run_state=subscribe),
            object(),
            7,
            12,
        )

    assert "assistant_sse_callback_cancelled" in {record.message for record in caplog.records}


@pytest.mark.asyncio
async def test_sse_callback_logs_and_propagates_failures(caplog: pytest.LogCaptureFixture) -> None:
    async def subscribe(_controller: object, _task_id: int, _run_id: int) -> None:
        raise RuntimeError("stream failed")

    with caplog.at_level("ERROR"), pytest.raises(RuntimeError, match="stream failed"):
        await TransportAssistantService.subscribe_run_state_with_logging(
            SimpleNamespace(subscribe_run_state=subscribe),
            object(),
            7,
            13,
        )

    assert "assistant_sse_callback_failed" in {record.message for record in caplog.records}
