"""Regression tests for the independent Task 5 final review blockers."""

from __future__ import annotations

import asyncio
import copy
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy.orm import sessionmaker

from app.assistant_transport.event import RunInitializedEvent
from app.assistant_transport.service.conversation_run_executor import ConversationRunExecutor
from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    empty_snapshot,
    validate_snapshot,
)
from app.core.workflows.workflow_operations import WorkflowOperations
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.task_record import TaskRecord
from app.service.provider.capability_service import CapabilityService
from app.service.task.conversation_run_service import ConversationRunService
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.engine_cache import create_sqlite_engine
from app.storage.init_schema import initialize_app_schema
from app.task_runtime.service.task_service import TaskService


def _timestamp(second: int = 0) -> datetime:
    return datetime(2026, 9, 12, 12, 0, second, tzinfo=UTC)


def _run(run_id: int, task_id: int = 7) -> ConversationRunRecord:
    return ConversationRunRecord(
        id=run_id,
        task_id=task_id,
        input_text=f"input-{run_id}",
        status="completed",
        created_at=_timestamp(run_id),
        updated_at=_timestamp(run_id),
        checkpoint_thread_id=f"thread-{run_id}",
        end_reason="done",
        final_output=f"output-{run_id}",
    )


def _task(task_id: int = 7) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        workspace_id=3,
        title="task",
        created_at=_timestamp(),
        updated_at=_timestamp(),
        current_run_id=None,
    )


def _metadata(parts: list[dict[str, object]], status: str = "success") -> dict[str, object]:
    return {
        "schema_version": 1,
        "parts": parts,
        "tool_result": {
            "status": status,
            "display_data": {"kind": "read-file-meta"},
            "status_hint": None,
            "error": None,
        },
    }


def _plain_metadata() -> dict[str, object]:
    """Return metadata valid for a non-tool context message."""

    return {"schema_version": 1, "parts": [], "tool_result": None}


def _call_metadata(parts: list[dict[str, object]]) -> dict[str, object]:
    """Return metadata valid for an AI message carrying tool-call parts."""

    return {"schema_version": 1, "parts": parts, "tool_result": None}


def _context_row(
    row_id: int,
    run_id: int,
    message: object,
    sequence: int,
    metadata: dict[str, object] | None = None,
) -> ConversationTaskContextRecord:
    return ConversationTaskContextRecord(
        id=row_id,
        task_id=7,
        run_id=run_id,
        message=message,
        include_in_context=True,
        sequence=sequence,
        transport_metadata=metadata or _metadata([]),
    )


@pytest.mark.asyncio
async def test_executor_persists_failed_run_before_nonfatal_tool_projector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    run = SimpleNamespace(id=1, task_id=7, status="running", end_reason=None)

    class RunService:
        def get_run(self, _run_id: int) -> SimpleNamespace:
            return run

        def fail_run_if_running(self, _run_id: int, end_reason: str | None = None) -> Any:
            order.append("run")
            run.status = "failed"
            run.end_reason = end_reason
            return run

        def complete_run_if_running(self, _run_id: int) -> None:
            return None

        def cancel_run_if_running(self, _run_id: int, end_reason: str = "user_cancelled") -> Any:
            order.append("run")
            run.status = "cancelled"
            run.end_reason = end_reason
            return run

    class FailingProjector:
        def process(self, _event: object) -> None:
            order.append("projector")
            raise RuntimeError("projector unavailable")

    executor = ConversationRunExecutor(run_service=RunService(), persist_status=False)
    executor._persist_status = True
    executor._event_projector = FailingProjector()

    async def runner(_run: object) -> None:
        raise RuntimeError("runner failed")

    await executor._run_and_settle(1, run, runner)

    assert run.status == "failed"
    assert order == ["run", "projector"]


@pytest.mark.asyncio
async def test_executor_persists_cancelled_run_before_nonfatal_tool_projector() -> None:
    order: list[str] = []
    run = SimpleNamespace(id=1, task_id=7, status="running", end_reason=None)

    class RunService:
        def get_run(self, _run_id: int) -> SimpleNamespace:
            return run

        def cancel_run_if_running(self, _run_id: int, end_reason: str = "user_cancelled") -> Any:
            order.append("run")
            run.status = "cancelled"
            run.end_reason = end_reason
            return run

        def complete_run_if_running(self, _run_id: int) -> None:
            return None

        def fail_run_if_running(self, _run_id: int, end_reason: str | None = None) -> Any:
            return None

    class FailingProjector:
        def process(self, _event: object) -> None:
            order.append("projector")
            raise RuntimeError("projector unavailable")

    executor = ConversationRunExecutor(run_service=RunService(), persist_status=False)
    executor._persist_status = True
    executor._event_projector = FailingProjector()

    async def runner(_run: object) -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await executor._run_and_settle(1, run, runner)

    assert run.status == "cancelled"
    assert order == ["run", "projector"]


def test_workflow_event_dispatcher_swallows_projector_failure() -> None:
    operations = WorkflowOperations.__new__(WorkflowOperations)

    class FailingProjector:
        def process(self, _event: object) -> None:
            raise RuntimeError("projector unavailable")

    operations._event_projector = FailingProjector()

    assert operations.process_event(RunInitializedEvent(task_id=7, run_id=11)) is None


def test_publish_state_rechecks_generation_after_validation_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ConversationTaskStateService.clear_process_state()
    state_service = ConversationTaskStateService(
        task_source=SimpleNamespace(get=lambda _task_id: _task(7)),
        run_source=SimpleNamespace(list_by_task=lambda _task_id: []),
        context_source=SimpleNamespace(get=lambda _task_id, **_kwargs: []),
    )
    canonical = empty_snapshot()
    state_service._rebuild = lambda _task_id: copy.deepcopy(canonical)  # type: ignore[method-assign]
    stale = empty_snapshot()
    stale["error"] = {"code": "stale", "message": "stale", "retryable": False}

    validation_started = threading.Event()
    release_validation = threading.Event()
    original_validate = validate_snapshot

    def blocking_validate(snapshot: Any) -> None:
        validation_started.set()
        assert release_validation.wait(5)
        original_validate(snapshot)

    monkeypatch.setattr(
        "app.assistant_transport.service.conversation_task_state_service.validate_snapshot",
        blocking_validate,
    )
    result: list[object] = []

    def publish() -> None:
        result.append(state_service.publish_state(7, stale))

    worker = threading.Thread(target=publish)
    worker.start()
    assert validation_started.wait(5)
    ConversationTaskStateService.clear_process_state()
    fresh_service = ConversationTaskStateService(
        task_source=SimpleNamespace(get=lambda _task_id: _task(7)),
        run_source=SimpleNamespace(list_by_task=lambda _task_id: []),
        context_source=SimpleNamespace(get=lambda _task_id, **_kwargs: []),
    )
    fresh_service._rebuild = lambda _task_id: copy.deepcopy(canonical)  # type: ignore[method-assign]
    release_validation.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(result) == 1
    assert task_runtime_spaces.get(7) is None
    assert fresh_service.get_state(7) == canonical


def test_direct_create_does_not_append_user_event_after_canonical_user_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    run = SimpleNamespace(id=11, task_id=7, input_text="hello")

    class RunCrud:
        def create(self, *_args: Any, **_kwargs: Any) -> Any:
            return run

    class TaskCrud:
        def set_current_run_id(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    class ContextService:
        def append_user_message_once(self, *_args: Any, **_kwargs: Any) -> bool:
            return True

    class Projector:
        def process(self, event: object, **_kwargs: Any) -> None:
            events.append(event)

    service = ConversationRunService.__new__(ConversationRunService)
    service._run = RunCrud()
    service._task = TaskCrud()
    service._context = ContextService()
    service._session_factory = None
    monkeypatch.setattr("app.service.depends.get_conversation_event_projector", lambda: Projector())

    service.create_run(7, "hello", agent_id="child_agent")

    assert [getattr(event, "type", None) for event in events] == ["run_initialized"]


def test_rebuilder_scopes_same_provider_tool_call_id_per_run() -> None:
    call_id = "provider-call-reused"
    tool_part = {
        "type": "tool-call",
        "toolCallId": call_id,
        "toolName": "read_file",
        "status": "pending",
        "args": {},
        "presentation": {},
        "isError": False,
    }
    rows = [
        _context_row(101, 11, HumanMessage(content="one"), 1, _plain_metadata()),
        _context_row(102, 11, AIMessage(content=""), 2, _call_metadata([tool_part])),
        _context_row(
            103,
            11,
            ToolMessage(content="one result", tool_call_id=call_id),
            3,
            _metadata([], "success"),
        ),
        _context_row(201, 12, HumanMessage(content="two"), 4, _plain_metadata()),
        _context_row(202, 12, AIMessage(content=""), 5, _call_metadata([tool_part])),
        _context_row(
            203,
            12,
            ToolMessage(content="two result", tool_call_id=call_id),
            6,
            _metadata([], "success"),
        ),
    ]

    task = _task()
    task.current_run_id = 12
    state = ConversationTaskStateRebuilder.rebuild(task, [_run(11), _run(12)], rows)

    validate_snapshot(state)
    assert all(
        part["status"] == "completed"
        for run in state["runs"]
        for message in run["messages"]
        for part in message["parts"]
        if part.get("type") == "tool-call"
    )


def test_task_service_reads_persisted_context_window_without_capability_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted_task = _task()
    persisted_task.context_window_total = 8192
    service = TaskService.__new__(TaskService)
    service._task = SimpleNamespace(get=lambda _task_id: persisted_task)
    service._turn = SimpleNamespace(
        get_latest_by_task=lambda _task_id: SimpleNamespace(model_name="unavailable-model")
    )
    monkeypatch.setattr(
        CapabilityService,
        "get_model_context_window",
        lambda _model: (_ for _ in ()).throw(
            AssertionError("cold read must not resolve capability")
        ),
    )

    assert service.get_context_window_total(7) == 8192


@pytest.fixture
def real_run_crud(tmp_path: Path) -> tuple[ConversationRunCrud, TaskRecord, Any]:
    engine = create_sqlite_engine(tmp_path / "storage" / "app.sqlite3")
    initialize_app_schema(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    workspace_crud = WorkspaceCrud.__new__(WorkspaceCrud)
    workspace_crud._session_factory = factory
    task_crud = TaskCrud.__new__(TaskCrud)
    task_crud._session_factory = factory
    run_crud = ConversationRunCrud.__new__(ConversationRunCrud)
    run_crud._session_factory = factory
    workspace = workspace_crud.create("test", str(tmp_path))
    task = task_crud.create(workspace.id, "task")
    try:
        yield run_crud, task, engine
    finally:
        engine.dispose()


def test_resume_cancelled_clears_usage_and_error(
    real_run_crud: tuple[ConversationRunCrud, TaskRecord, Any],
) -> None:
    run_crud, task, _engine = real_run_crud
    usage = {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
        "cache_hit_tokens": 1,
        "cache_miss_tokens": 1,
        "reasoning_tokens": 0,
    }
    error = {"code": "old_failure", "message": "old", "retryable": False}
    run = run_crud.create(
        task.id,
        "input",
        status="cancelled",
        usage=usage,
        error=error,
        session=None,
    )

    resumed = run_crud.resume_cancelled(run.id)

    assert resumed is not None
    assert resumed.status == "running"
    assert resumed.end_reason is None
    assert resumed.final_output is None
    assert resumed.usage is None
    assert resumed.error is None
