"""Terminal session lifecycle projection regressions for Assistant Transport."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.core.llm_provider.messages import AIMessage, ToolMessage
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.task_record import TaskRecord
from app.service.terminal.terminal_session_status import TerminalSessionStatusChange
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
    )


def _rows(
    status: str = "running", *, call_id: str = "terminal-call"
) -> list[ConversationTaskContextRecord]:
    return [
        ConversationTaskContextRecord(
            task_id=1,
            run_id=10,
            message=AIMessage(
                content="",
                tool_calls=[{"id": call_id, "name": "terminal_start", "args": {}}],
            ),
            include_in_context=True,
            sequence=1,
            transport_metadata={},
        ),
        ConversationTaskContextRecord(
            task_id=1,
            run_id=10,
            message=ToolMessage(content="started", tool_call_id=call_id),
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
    ) -> TerminalSessionStatusChange | None:
        if task_id != 1 or session_id != "term-1":
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
        assert task_id == 1 and run_id in {10, 11} and session_id == "term-1"
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
    parts = state["runs"][0]["messages"][0]["parts"]
    assert next(part for part in parts if part["type"] == "tool-call")["status"] == "completed"
    assert sources.updates == [("closed", "backend_restarted", None)]


def test_stale_active_terminal_event_cannot_reopen_closed_display() -> None:
    sources = _Sources(_rows())
    service = _service(sources)
    service.get_state(1)

    service.refresh_terminal_session(
        TerminalSessionStatusChange(
            task_id=1,
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


# 本文件全部用例都需要真实后端环境（隔离主库 + 进程级单例）。
pytestmark = pytest.mark.usefixtures("backend_env")


def test_cross_run_references_reconcile_and_receive_async_exit(monkeypatch) -> None:
    """同一 Task 两个 Run 引用一个 session，冷重建与异步通知均按 session 归属。"""
    from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

    sources = _Sources(_rows())
    later = _rows()
    later = [replace(row, run_id=11, sequence=row.sequence + 2) for row in later]
    sources.rows.extend(later)
    monkeypatch.setattr(sources, "list_by_task", lambda _: [_run(), replace(_run(), id=11)])
    service = _service(sources)
    state = service.get_state(1)
    displays = [
        part["display_data"]
        for run in state["runs"]
        for message in run["messages"]
        for part in message["parts"]
        if part.get("display_data", {}).get("session_id") == "term-1"
    ]
    assert len(displays) == 2
    assert all(display["status"] == "running" for display in displays)
    service.refresh_terminal_session(replace(sources.status_change, status="exited", exit_code=0))
    current = task_runtime_spaces.get(1).existing_snapshot()
    displays = [
        part["display_data"]
        for run in current["runs"]
        for message in run["messages"]
        for part in message["parts"]
        if part.get("display_data", {}).get("session_id") == "term-1"
    ]
    assert all(display["status"] == "exited" for display in displays)
    assert [
        row.transport_metadata["display_data"]["status"]
        for row in sources.rows
        if "display_data" in row.transport_metadata
    ] == ["exited", "exited"]


def test_cross_run_projection_persists_and_reconciles_late_rows_in_sqlite(monkeypatch) -> None:
    """跨 Run 更新真实存储；退出后才落盘的工具结果在冷重建时补齐，其他 Task 不受影响。"""
    sources = _Sources(_rows())
    monkeypatch.setattr(sources, "list_by_task", lambda _: [_run(), replace(_run(), id=11)])
    service = _service(sources)
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    StorageBase.metadata.create_all(engine)
    crud = ConversationTaskContextCrud.__new__(ConversationTaskContextCrud)
    crud._session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    service._context_source = crud
    try:
        for row in _rows():
            crud.create(row)
            crud.create(replace(row, run_id=11, sequence=row.sequence + 2))
            crud.create(replace(row, task_id=2))
        service.get_state(1)
        exited = replace(
            sources.status_change, status="exited", end_reason="shell_exited", exit_code=7
        )
        sources.status_change = exited
        service.refresh_terminal_session(exited)

        def persisted_displays(task_id):
            return [
                row.transport_metadata["display_data"]
                for row in crud.get(task_id, include_in_context=False)
                if "display_data" in row.transport_metadata
            ]

        assert [item["exit_code"] for item in persisted_displays(1)] == [7, 7]
        assert [item["status"] for item in persisted_displays(2)] == ["running"]
        # 同一 Run 的另一次工具读取可能晚于退出通知持久化，保留当时的 running 快照。
        for row in _rows(call_id="late-terminal-call"):
            crud.create(replace(row, run_id=11, sequence=row.sequence + 4))
        state = service.rebuild_state(1)
        displays = [
            part["display_data"]
            for run in state["runs"]
            for message in run["messages"]
            for part in message["parts"]
            if part.get("display_data", {}).get("session_id") == "term-1"
        ]
        assert len(displays) == 3
        assert all(item["status"] == "exited" and item["exit_code"] == 7 for item in displays)
        assert [item["exit_code"] for item in persisted_displays(1)] == [7, 7, 7]
        service.refresh_terminal_session(
            replace(exited, status="running", end_reason=None, exit_code=None)
        )
        assert all(item["status"] == "exited" for item in persisted_displays(1))
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _task_space_sources(monkeypatch):
    """展示测试显式装配 Task 空间读取源，避免依赖真实主库。"""
    from types import SimpleNamespace

    from app.task_runtime import task_runtime_space as space_module
    from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

    task_runtime_spaces.close()
    monkeypatch.setattr(
        space_module, "get_task_service", lambda: SimpleNamespace(get_task=lambda _: _task())
    )
    monkeypatch.setattr(
        space_module,
        "get_workspace_service",
        lambda: SimpleNamespace(get_workspace=lambda _: SimpleNamespace(id=1)),
    )
    yield
    task_runtime_spaces.close()
