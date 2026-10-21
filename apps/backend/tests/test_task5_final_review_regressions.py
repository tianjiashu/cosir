"""Regression tests for the independent Task 5 final review blockers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import sessionmaker

from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    validate_snapshot,
)
from app.core.events import RunInitializedEvent
from app.models.conversation_run_command import ConversationRunCommand
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.models.message_metadata import parse_message_metadata
from app.models.task_record import TaskRecord
from app.service.conversation_run.conversation_run_service import ConversationRunService
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.engine_cache import create_sqlite_engine
from app.storage.store_engines import initialize_app_schema


def _timestamp(second: int = 0) -> datetime:
    return datetime(2026, 9, 12, 12, 0, second, tzinfo=UTC)


def _run(run_id: int, task_id: int = 7) -> ConversationRunRecord:
    return ConversationRunRecord(
        id=run_id,
        task_id=task_id,
        status="completed",
        created_at=_timestamp(run_id),
        updated_at=_timestamp(run_id),
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
        metadata=parse_message_metadata(message["role"], metadata or {}),
    )


def test_workflow_event_dispatcher_swallows_projector_failure() -> None:
    from app.core.workflow.node_helper.common import emit, execution_scope

    def failing_sink(_event: object) -> None:
        raise RuntimeError("projector unavailable")

    with execution_scope(SimpleNamespace(event_sink=failing_sink), SimpleNamespace()):
        emit(RunInitializedEvent(task_id=7, run_id=11))


def test_direct_create_does_not_append_user_event_after_canonical_user_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    run = SimpleNamespace(id=11, task_id=7, context_window_total=128_000)

    class RunCrud:
        def create(self, *_args: Any, **_kwargs: Any) -> Any:
            return run

    class TaskCrud:
        def set_current_run_id(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    class Projector:
        def process(self, event: object, **_kwargs: Any) -> None:
            events.append(event)

    service = ConversationRunService.__new__(ConversationRunService)
    service._run = RunCrud()
    service._task = TaskCrud()
    from contextlib import nullcontext

    from app.storage.crud.task_input_crud import TaskInputCrud

    service._session_factory = SimpleNamespace(begin=lambda: nullcontext(object()))
    queued = []
    monkeypatch.setattr(TaskInputCrud, "append", lambda self, *args: queued.append(args))
    monkeypatch.setattr(TaskInputCrud, "release_all_immediate", lambda self, *args: None)
    monkeypatch.setattr(TaskInputCrud, "list_pending", lambda self, *args: list(queued))
    monkeypatch.setattr("app.service.depends.get_conversation_event_projector", lambda: Projector())
    monkeypatch.setattr(
        "app.service.conversation_run.conversation_run_service.get_model_config_service",
        lambda: SimpleNamespace(
            get_config=lambda _config_id: SimpleNamespace(
                supports_reasoning_effort=False,
                supports_image=False,
                context_window_k=128,
            )
        ),
    )
    monkeypatch.setattr(
        "app.service.conversation_run.conversation_input_preparation_service.get_model_config_service",
        lambda: SimpleNamespace(
            get_config=lambda _config_id: SimpleNamespace(
                supports_reasoning_effort=False,
                supports_image=False,
                context_window_k=128,
            )
        ),
    )

    from app.service.conversation_run.conversation_input_preparation_service import (
        ConversationInputPreparationService,
    )

    preparation = ConversationInputPreparationService.__new__(ConversationInputPreparationService)
    preparation._task = SimpleNamespace(get=lambda _id: SimpleNamespace(workspace_id=None))
    monkeypatch.setattr(
        "app.service.conversation_run.conversation_run_service.ConversationInputPreparationService",
        lambda: preparation,
    )
    service.create_run(
        7,
        model_config_id=1,
        run_command=ConversationRunCommand(display_text="hello"),
        agent_id="child_agent",
    )

    assert [getattr(event, "type", None) for event in events] == ["run_initialized"]


def test_rebuilder_scopes_same_provider_tool_call_id_per_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 重建按 AI 消息的 tool_calls 配对工具结果；tool registry 未在本测试初始化，给出最小展示。
    monkeypatch.setattr(
        ConversationTaskStateRebuilder,
        "get_tool_display",
        staticmethod(lambda _name: {"verb": "Read"}),
    )
    call_id = "provider-call-reused"
    rows = [
        _context_row(101, 11, {"role": "user", "content": "one"}, 1),
        _context_row(
            102,
            11,
            {
                "role": "assistant",
                "content": "answer",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "read_file", "arguments": json.dumps({})},
                    }
                ],
                "reasoning_content": None,
            },
            2,
        ),
        _context_row(
            103,
            11,
            {"role": "tool", "content": "one result", "tool_call_id": call_id},
            3,
            {
                "status": "completed",
                "display_data": {"kind": "read-file-meta"},
                "error": None,
            },
        ),
        _context_row(201, 12, {"role": "user", "content": "two"}, 4),
        _context_row(
            202,
            12,
            {
                "role": "assistant",
                "content": "answer",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "read_file", "arguments": json.dumps({})},
                    }
                ],
                "reasoning_content": None,
            },
            5,
        ),
        _context_row(
            203,
            12,
            {"role": "tool", "content": "two result", "tool_call_id": call_id},
            6,
            {
                "status": "completed",
                "display_data": {"kind": "read-file-meta"},
                "error": None,
            },
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


def test_rebuild_reads_run_owned_context_windows_without_capability_lookup() -> None:
    runs = [_run(11), _run(12)]
    runs[0].context_window_total = 8192
    runs[1].context_window_total = 16384
    state = ConversationTaskStateRebuilder.rebuild(_task(), runs, [])
    assert [run["contextWindowTotal"] for run in state["runs"]] == [8192, 16384]


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


def test_resume_cancelled_run_clears_terminal_fields_and_preserves_progress(
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
    error = {"code": "old_failure", "message": "old"}
    run = run_crud.create(
        task.id,
        status="cancelled",
        usage=usage,
        error=error,
        session=None,
    )

    # 续跑清除终态字段，保留模型边界的累计进度。
    resumed = run_crud.update_status_if_in(
        run.id,
        ConversationRunStatus.RUNNING.value,
        (ConversationRunStatus.CANCELLED.value,),
        clear_terminal_fields=True,
    )

    assert resumed is not None
    assert resumed.status == "running"
    assert resumed.end_reason is None
    assert resumed.final_output is None
    assert resumed.usage is None
    assert resumed.error is None
    assert resumed.execution_data == run.execution_data
