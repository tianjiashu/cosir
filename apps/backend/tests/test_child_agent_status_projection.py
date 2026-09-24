"""Child Run lifecycle projection regressions for parent Transport state."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.task_record import TaskRecord
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
from app.storage.model.base import StorageBase
from app.storage.model.conversation_task_context_model import ConversationTaskContextModel

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def _task(
    task_id: int,
    *,
    parent_task_id: int | None = None,
    current_run_id: int | None = None,
) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        workspace_id=1,
        title=f"task-{task_id}",
        created_at=NOW,
        updated_at=NOW,
        parent_task_id=parent_task_id,
        current_run_id=current_run_id,
    )


def _run(run_id: int, task_id: int, status: str) -> ConversationRunRecord:
    return ConversationRunRecord(
        id=run_id,
        task_id=task_id,
        input_text=f"input-{run_id}",
        status=status,
        created_at=NOW,
        updated_at=NOW,
        checkpoint_thread_id=f"checkpoint-{run_id}",
    )


def _tool_rows(*displays: tuple[str, dict[str, object]]) -> list[ConversationTaskContextRecord]:
    calls = [
        {
            "id": tool_call_id,
            "name": tool_name,
            "args": {},
        }
        for tool_call_id, tool_name in (
            ("delegate-call", "delegate_task"),
            ("send-call", "child_agent_send"),
            ("status-call", "child_agent_status"),
        )
    ]
    rows: list[ConversationTaskContextRecord] = [
        ConversationTaskContextRecord(
            task_id=1,
            run_id=10,
            message=AIMessage(content="", tool_calls=calls),
            include_in_context=True,
            sequence=1,
            transport_metadata={},
        )
    ]
    for sequence, (call, (_, display)) in enumerate(
        zip(calls, displays, strict=True),
        start=2,
    ):
        tool_call_id = call["id"]
        rows.append(
            ConversationTaskContextRecord(
                task_id=1,
                run_id=10,
                message=ToolMessage(content="result", tool_call_id=tool_call_id),
                include_in_context=True,
                sequence=sequence,
                transport_metadata={
                    "status": "completed",
                    "display_data": display,
                },
            )
        )
    return rows


class _Sources:
    def __init__(self, rows: list[ConversationTaskContextRecord]) -> None:
        self.tasks = {
            1: _task(1, current_run_id=10),
            2: _task(2, parent_task_id=1, current_run_id=20),
        }
        self.runs = {
            1: [_run(10, 1, "completed")],
            2: [_run(20, 2, "running"), _run(21, 2, "running")],
        }
        self.rows = rows
        self.metadata_updates: list[tuple[int, int, str]] = []

    def get(
        self,
        task_id: int,
        include_in_context: bool = True,
    ) -> TaskRecord | list[ConversationTaskContextRecord]:
        if include_in_context:
            return self.tasks[task_id]
        return list(self.rows)

    def list_by_task(self, task_id: int) -> list[ConversationRunRecord]:
        return list(self.runs[task_id])

    def update_child_display_status(
        self,
        task_id: int,
        sequence: int,
        child_task_id: int,
        child_run_id: int,
        status: str,
    ) -> None:
        self.metadata_updates.append((task_id, sequence, status))
        for index, row in enumerate(self.rows):
            if row.task_id == task_id and row.sequence == sequence:
                metadata = dict(row.transport_metadata or {})
                display = dict(metadata.get("display_data") or {})
                if (
                    display.get("child_task_id") == child_task_id
                    and display.get("child_run_id") == child_run_id
                ):
                    display["status"] = status
                    metadata["display_data"] = display
                    self.rows[index] = replace(row, transport_metadata=metadata)


def _service(sources: _Sources) -> ConversationTaskStateService:
    service = ConversationTaskStateService.__new__(ConversationTaskStateService)
    service._task_source = sources
    service._run_source = sources
    service._context_source = sources
    return service


def _display_by_call(state: dict[str, object], tool_call_id: str) -> dict[str, object]:
    for run in state["runs"]:  # type: ignore[index]
        for message in run["messages"]:
            for part in message["parts"]:
                if part.get("toolCallId") == tool_call_id:
                    return part["display_data"]
    raise AssertionError(f"tool call not found: {tool_call_id}")


@pytest.fixture(autouse=True)
def _reset_task_runtime_spaces(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.assistant_transport.service.conversation_task_state_rebuilder import (
        ConversationTaskStateRebuilder,
    )

    monkeypatch.setattr(
        ConversationTaskStateRebuilder,
        "get_tool_display",
        staticmethod(lambda _name: {}),
    )
    ConversationTaskStateService.clear_process_state()
    yield
    ConversationTaskStateService.clear_process_state()


def test_cold_rebuild_reconciles_live_child_statuses_by_exact_run_locator() -> None:
    rows = _tool_rows(
        ("delegate-call", {
            "kind": "delegation-result",
            "title": "Review",
            "child_task_id": 2,
            "child_run_id": 20,
            "status": "running",
        }),
        ("send-call", {
            "kind": "child-agent-result",
            "operation": "send",
            "child_task_id": 2,
            "child_run_id": 21,
            "status": "running",
        }),
        ("status-call", {
            "kind": "child-agent-result",
            "operation": "status",
            "child_task_id": 2,
            "child_run_id": 20,
            "status": "running",
        }),
    )
    sources = _Sources(rows)
    sources.runs[2][0].status = "completed"
    sources.runs[2][1].status = "failed"

    state = _service(sources).get_state(1)

    assert _display_by_call(state, "delegate-call")["status"] == "completed"
    assert _display_by_call(state, "send-call")["status"] == "failed"
    # status is a point-in-time query result, not a live child status card.
    assert _display_by_call(state, "status-call")["status"] == "running"
    assert sources.metadata_updates == []


def test_live_refresh_updates_delegate_and_send_without_overwriting_old_run() -> None:
    rows = _tool_rows(
        ("delegate-call", {
            "kind": "delegation-result",
            "title": "Review",
            "child_task_id": 2,
            "child_run_id": 20,
            "status": "running",
        }),
        ("send-call", {
            "kind": "child-agent-result",
            "operation": "send",
            "child_task_id": 2,
            "child_run_id": 21,
            "status": "running",
        }),
        ("status-call", {
            "kind": "child-agent-result",
            "operation": "status",
            "child_task_id": 2,
            "child_run_id": 20,
            "status": "running",
        }),
    )
    sources = _Sources(rows)
    service = _service(sources)
    state = service.get_state(1)
    assert _display_by_call(state, "delegate-call")["status"] == "running"

    sources.runs[2][0].status = "completed"
    service.refresh_parent_delegation(2, 20, "running")
    state = service.get_state(1)
    assert _display_by_call(state, "delegate-call")["status"] == "completed"
    assert _display_by_call(state, "send-call")["status"] == "running"

    sources.runs[2][1].status = "cancelled"
    service.refresh_parent_delegation(2, 21, "running")
    state = service.get_state(1)
    assert _display_by_call(state, "delegate-call")["status"] == "completed"
    assert _display_by_call(state, "send-call")["status"] == "cancelled"
    assert [sequence for _, sequence, _ in sources.metadata_updates] == [2, 3]
    assert [status for _, _, status in sources.metadata_updates] == ["completed", "cancelled"]


def test_event_projection_persists_status_when_parent_tool_row_arrives_after_child_completion(
) -> None:
    rows = _tool_rows(
        ("delegate-call", {
            "kind": "delegation-result",
            "title": "Review",
            "child_task_id": 2,
            "child_run_id": 20,
            "status": "running",
        }),
        ("send-call", {
            "kind": "child-agent-result",
            "operation": "send",
            "child_task_id": 2,
            "child_run_id": 21,
            "status": "running",
        }),
        ("status-call", {
            "kind": "child-agent-result",
            "operation": "status",
            "child_task_id": 2,
            "child_run_id": 20,
            "status": "running",
        }),
    )
    sources = _Sources(rows)
    service = _service(sources)
    service.get_state(1)

    sources.runs[2][0].status = "completed"
    change = service.apply_planned(
        SimpleNamespace(task_id=1, run_id=10, plan=lambda _state: [])
    )

    assert change.mutations
    assert _display_by_call(service.get_state(1), "delegate-call")["status"] == "completed"
    assert [sequence for _, sequence, _ in sources.metadata_updates] == [2]


def test_context_crud_updates_only_child_display_status_and_keeps_concurrent_metadata() -> None:
    engine = create_engine("sqlite://")
    StorageBase.metadata.create_all(engine)
    try:
        crud = ConversationTaskContextCrud.__new__(ConversationTaskContextCrud)
        metadata = {
            "status": "completed",
            "display_data": {
                "kind": "delegation-result",
                "child_task_id": 2,
                "child_run_id": 20,
                "status": "running",
            },
            "other_field": "must-survive",
        }
        with Session(engine) as session:
            session.add(
                ConversationTaskContextModel(
                    task_id=1,
                    run_id=10,
                    message_json=json.dumps({"type": "ai", "data": {"content": "old"}}),
                    include_in_context=True,
                    sequence=2,
                    transport_metadata_json=json.dumps(metadata),
                )
            )
            session.flush()

            crud.update_child_display_status(1, 2, 2, 20, "failed", session=session)
            row = session.scalar(
                select(ConversationTaskContextModel).where(
                    ConversationTaskContextModel.task_id == 1,
                    ConversationTaskContextModel.sequence == 2,
                )
            )
            assert row is not None
            updated = json.loads(row.transport_metadata_json)
            assert updated["display_data"]["status"] == "failed"
            assert updated["other_field"] == "must-survive"

            crud.replace_message(
                1,
                2,
                ConversationTaskContextRecord(
                    task_id=1,
                    run_id=10,
                    message=AIMessage(content="new"),
                    include_in_context=True,
                    sequence=2,
                ),
                session=session,
            )
            row = session.scalar(
                select(ConversationTaskContextModel).where(
                    ConversationTaskContextModel.task_id == 1,
                    ConversationTaskContextModel.sequence == 2,
                )
            )
            assert row is not None
            assert json.loads(row.transport_metadata_json) == updated
    finally:
        engine.dispose()
