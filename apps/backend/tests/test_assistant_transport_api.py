import asyncio
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute

from app.app import app
from app.assistant_transport.assistant_api import _subscribe_run_state_with_logging


def test_resume_state_route_does_not_build_a_union_response_model() -> None:
    route = next(
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path == "/tasks/{task_id}/assistant/resume-state"
    )

    # The endpoint returns 204 for an inactive run and JSONResponse for an
    # active run.  FastAPI must not interpret those two wire shapes as a
    # Pydantic ``Response | dict`` response model during app import.
    assert route.response_model is None
    assert route.response_field is None


@pytest.mark.asyncio
async def test_sse_callback_logs_and_returns_on_normal_completion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def subscribe(_controller: object, _task_id: int, _run_id: int) -> None:
        return None

    with caplog.at_level("INFO"):
        await _subscribe_run_state_with_logging(
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
        await _subscribe_run_state_with_logging(
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
        await _subscribe_run_state_with_logging(
            SimpleNamespace(subscribe_run_state=subscribe),
            object(),
            7,
            13,
        )

    assert "assistant_sse_callback_failed" in {record.message for record in caplog.records}
