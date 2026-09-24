"""Terminal session lifecycle projection regressions for Assistant Transport."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.task_record import TaskRecord
from app.service.terminal.session_status import TerminalSessionStatusChange
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
from app.storage.model.base import StorageBase
from app.storage.model.conversation_task_context_model import ConversationTaskContextModel

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def _task() -> TaskRecord:
    return TaskRecord(
        id=1,
        workspace_id=1,
        title="terminal task",
        created_at=NOW,
        updated_at=NOW,
        current_run_id=10,
    )


def _run() -> ConversationRunRecord:
    return ConversationRunRecord(
        id=10,
        task_id=1,
        input_text="start terminal",
        status="completed",
        created_at=NOW,
        updated_at=NOW,
        checkpoint_thread_id="checkpoint-10",
    )


def _rows(status: str = "running") -> list[ConversationTaskContextRecord]:
    return [
        ConversationTaskContextRecord(
            task_id=1,
            run_id=10,
            message=AIMessage(
                content="",
                tool_calls=[
                    {"id": "terminal-call", "name": "terminal_start", "args": {}}
                ],
            ),
            include_in_context=True,
            sequence=1,
            transport_metadata={},
        ),
        ConversationTaskContextRecord(
            task_id=1,
            run_id=10,
            message=ToolMessage(content="started", tool_call_id="terminal-call"),
            include_in_context=True,
            sequence=2,
            transport_metadata={
                "status": "completed",
                "display_data": {
                    "kind": "terminal-session",
                    "session_id": "term-1",
                    "status": status,
                    "shell_kind": "powershell",
                },
            },
        ),
    ]


class _Sources:
    def __init__(self, rows: list[ConversationTaskContextRecord]) -> None:
        self.task = _task()
        self.rows = rows
        self.status_change: TerminalSessionStatusChange | None = TerminalSessionStatusChange(
            task_id=1,
            run_id=10,
            session_id="term-1",
            generation="gen-1",
            status="running",
            end_reason=None,
            exit_code=None,
        )
        self.updates: list[tuple[str, str, int | None]] = []

    def get(
        self,
        task_id: int,
        include_in_context: bool = True,
    ) -> TaskRecord | list[ConversationTaskContextRecord]:
        assert task_id == 1
        return self.task if include_in_context else list(self.rows)

    def list_by_task(self, task_id: int) -> list[ConversationRunRecord]:
        assert task_id == 1
        return [_run()]

    def get_status_change(
        self,
        session_id: str,
        *,
        task_id: int,
        run_id: int,
    ) -> TerminalSessionStatusChange | None:
        if task_id != 1 or run_id != 10 or session_id != "term-1":
            return None
        return self.status_change

    def update_terminal_session_display(
        self,
        task_id: int,
        sequence: int,
        run_id: int,
        session_id: str,
        status: str,
        end_reason: str | None,
        exit_code: int | None,
    ) -> None:
        assert (task_id, run_id, session_id) == (1, 10, "term-1")
        self.updates.append((status, end_reason or "", exit_code))
        for index, row in enumerate(self.rows):
            if row.sequence != sequence:
                continue
            metadata = dict(row.transport_metadata or {})
            display = dict(metadata["display_data"])
            display.update(
                status=status,
                end_reason=end_reason,
                exit_code=exit_code,
            )
            metadata["display_data"] = display
            self.rows[index] = replace(row, transport_metadata=metadata)


def _service(sources: _Sources) -> ConversationTaskStateService:
    service = ConversationTaskStateService.__new__(ConversationTaskStateService)
    service._task_source = sources
    service._run_source = sources
    service._context_source = sources
    service._terminal_status_source = sources
    return service


def _display(state: dict[str, object]) -> dict[str, object]:
    for run in state["runs"]:  # type: ignore[index]
        for message in run["messages"]:
            for part in message["parts"]:
                if part.get("toolCallId") == "terminal-call":
                    return part["display_data"]
    raise AssertionError("terminal tool part not found")


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ConversationTaskStateRebuilder,
        "get_tool_display",
        staticmethod(lambda _name: {}),
    )
    ConversationTaskStateService.clear_process_state()
    yield
    ConversationTaskStateService.clear_process_state()


def test_async_terminal_exit_updates_existing_snapshot_and_persists_metadata() -> None:
    sources = _Sources(_rows())
    service = _service(sources)

    state = service.get_state(1)
    assert _display(state)["status"] == "running"

    frame = service.refresh_terminal_session(
        TerminalSessionStatusChange(
            task_id=1,
            run_id=10,
            session_id="term-1",
            generation="gen-1",
            status="exited",
            end_reason="shell_exited",
            exit_code=0,
        )
    )

    assert frame is not None
    assert _display(service.get_state(1)) == {
        "kind": "terminal-session",
        "session_id": "term-1",
        "status": "exited",
        "shell_kind": "powershell",
        "end_reason": "shell_exited",
        "exit_code": 0,
    }
    assert sources.updates == [("exited", "shell_exited", 0)]


def test_cold_rebuild_closes_orphaned_running_session_and_keeps_tool_completed() -> None:
    sources = _Sources(_rows())
    sources.status_change = None
    service = _service(sources)

    state = service.get_state(1)
    display = _display(state)

    assert display["status"] == "closed"
    assert display["end_reason"] == "backend_restarted"
    assert state["runs"][0]["messages"][1]["parts"][0]["status"] == "completed"  # type: ignore[index]
    assert sources.updates == [("closed", "backend_restarted", None)]


def test_stale_active_terminal_event_cannot_reopen_closed_display() -> None:
    sources = _Sources(_rows())
    service = _service(sources)
    service.get_state(1)

    service.refresh_terminal_session(
        TerminalSessionStatusChange(
            task_id=1,
            run_id=10,
            session_id="term-1",
            generation="gen-1",
            status="closed",
            end_reason="run_finished",
            exit_code=None,
        )
    )
    service.refresh_terminal_session(
        TerminalSessionStatusChange(
            task_id=1,
            run_id=10,
            session_id="term-1",
            generation="gen-old",
            status="running",
            end_reason=None,
            exit_code=None,
        )
    )

    assert _display(service.get_state(1))["status"] == "closed"
    assert sources.updates == [("closed", "run_finished", None)]


def test_terminal_metadata_update_is_atomic_and_preserves_other_transport_fields() -> None:
    engine = create_engine("sqlite://")
    StorageBase.metadata.create_all(engine)
    try:
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        crud = ConversationTaskContextCrud.__new__(ConversationTaskContextCrud)
        crud._session_factory = factory
        with Session(engine) as session:
            session.add(
                ConversationTaskContextModel(
                    task_id=1,
                    run_id=10,
                    sequence=2,
                    message_json=json.dumps({"type": "tool"}),
                    transport_metadata_json=json.dumps(
                        {
                            "display_data": {
                                "kind": "terminal-session",
                                "session_id": "term-1",
                                "status": "running",
                            },
                            "concurrent_field": "preserve",
                        },
                    ),
                )
            )
            session.commit()

        crud.update_terminal_session_display(
            1,
            2,
            10,
            "term-1",
            "closed",
            "backend_shutdown",
            None,
        )

        crud.update_terminal_session_display(
            1,
            2,
            10,
            "term-1",
            "exited",
            "shell_exited",
            0,
        )

        with Session(engine) as session:
            metadata_json = session.scalar(
                select(ConversationTaskContextModel.transport_metadata_json)
            )
        assert metadata_json is not None
        assert json.loads(metadata_json) == {
            "display_data": {
                "kind": "terminal-session",
                "session_id": "term-1",
                "status": "closed",
                "end_reason": "backend_shutdown",
                "exit_code": None,
            },
            "concurrent_field": "preserve",
        }
    finally:
        engine.dispose()
