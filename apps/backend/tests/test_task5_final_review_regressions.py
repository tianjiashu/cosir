"""Regression tests for the independent Task 5 final review blockers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import sessionmaker

from app.assistant_transport.projection.snapshot_rebuild import (
    rebuild_snapshot,
)
from app.core.events import RunInitializedEvent
from app.core.workflow.run_ledger import RunLedger
from app.models.enums.run_status import RunStatus
from app.models.message_metadata import parse_message_metadata
from app.models.run_record import RunRecord
from app.models.session_context import SessionContextRecord
from app.models.session_record import SessionRecord
from app.storage.crud.run_crud import RunCrud
from app.storage.crud.session_crud import SessionCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.engine_cache import create_sqlite_engine
from app.storage.store_engines import initialize_app_schema

# RunInitializedEvent 的 created_at 由领域侧从 Run 记录填入；测试给固定值。
_RUN_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _timestamp(second: int = 0) -> datetime:
    return datetime(2026, 9, 12, 12, 0, second, tzinfo=UTC)


def _run(run_id: int, session_id: int = 7) -> RunRecord:
    return RunRecord(
        id=run_id,
        session_id=session_id,
        status="completed",
        created_at=_timestamp(run_id),
        updated_at=_timestamp(run_id),
        end_reason="done",
        final_output=f"output-{run_id}",
    )


def _session(session_id: int = 7) -> SessionRecord:
    return SessionRecord(
        id=session_id,
        workspace_id=3,
        title="session",
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
) -> SessionContextRecord:
    return SessionContextRecord(
        id=row_id,
        session_id=7,
        run_id=run_id,
        # 用户行的 wire 身份来自入队消息 id；测试直接复用行 id。
        message_id=row_id if message["role"] == "user" else None,
        message=message,
        include_in_context=True,
        sequence=sequence,
        metadata=parse_message_metadata(
            message["role"],
            metadata
            # 落库的 user 行必须带展示绑定；测试用正文作展示文本。
            or (
                {"kind": "user_input", "presentation": {"text": message["content"]}}
                if message["role"] == "user"
                else {}
            ),
        ),
        turn=1,
    )


def test_workflow_event_dispatcher_swallows_projector_failure() -> None:
    from app.core.workflow.node_helper.common import emit, execution_scope

    def failing_sink(_event: object) -> None:
        raise RuntimeError("projector unavailable")

    with execution_scope(SimpleNamespace(event_sink=failing_sink), SimpleNamespace()):
        emit(RunInitializedEvent(created_at=_RUN_CREATED_AT, session_id=7, run_id=11))


def test_direct_create_does_not_append_user_event_after_canonical_user_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    run = SimpleNamespace(
        id=11,
        session_id=7,
        context_window_total=128_000,
        model_config_id="test/m1",
        reasoning_effort=None,
        created_at=_RUN_CREATED_AT,
    )

    class FakeRunCrud:
        def create(self, *_args: Any, **_kwargs: Any) -> Any:
            return run

    class SessionCrud:
        def set_current_run_id(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    class Projector:
        def process(self, event: object, **_kwargs: Any) -> None:
            events.append(event)

    service = RunLedger.__new__(RunLedger)
    service._run = FakeRunCrud()
    service._session = SessionCrud()
    from contextlib import nullcontext

    from app.core.workflow.message_queue.session_message import RunLaunch

    service._db_factory = SimpleNamespace(begin=lambda: nullcontext(object()))
    monkeypatch.setattr(
        "app.service.depends.get_conversation_event_projector",
        lambda: Projector(),
    )
    monkeypatch.setattr(
        "app.core.workflow.run_ledger.get_model_config_service",
        lambda: SimpleNamespace(
            get_config=lambda _config_id: SimpleNamespace(
                model=SimpleNamespace(
                    supports_reasoning_effort=False,
                    supports_image=False,
                    context_window_k=128,
                )
            )
        ),
    )
    service.create_run(7, RunLaunch(agent_id="child_agent", model_config_id="test/m1"))

    assert [getattr(event, "type", None) for event in events] == ["run_initialized"]


def test_rebuild_reads_run_owned_context_windows_without_capability_lookup() -> None:
    runs = [_run(11), _run(12)]
    runs[0].context_window_total = 8192
    runs[1].context_window_total = 16384
    session = _session()
    session.current_run_id = 12
    state = rebuild_snapshot(session, runs, [], {})
    # 只有最新 Run 的冻结窗口进入 activity；历史 Run 的窗口不进 wire。
    assert state["activity"] is not None
    assert state["activity"]["contextWindowTotal"] == 16384


@pytest.fixture
def real_run_crud(tmp_path: Path) -> tuple[RunCrud, SessionRecord, Any]:
    engine = create_sqlite_engine(tmp_path / "storage" / "app.sqlite3")
    initialize_app_schema(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    workspace_crud = WorkspaceCrud.__new__(WorkspaceCrud)
    workspace_crud._db_factory = factory
    session_crud = SessionCrud.__new__(SessionCrud)
    session_crud._db_factory = factory
    run_crud = RunCrud.__new__(RunCrud)
    run_crud._db_factory = factory
    workspace = workspace_crud.create("test", str(tmp_path))
    session = session_crud.create(workspace.id, "session")
    try:
        yield run_crud, session, engine
    finally:
        engine.dispose()


def test_resume_cancelled_run_clears_terminal_fields_and_preserves_progress(
    real_run_crud: tuple[RunCrud, SessionRecord, Any],
) -> None:
    run_crud, session, _engine = real_run_crud
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
        session.id,
        status="cancelled",
        usage=usage,
        error=error,
        db=None,
    )

    # 续跑清除终态字段，保留模型边界的累计进度。
    resumed = run_crud.update_status_if_in(
        run.id,
        RunStatus.RUNNING.value,
        (RunStatus.CANCELLED.value,),
        clear_terminal_fields=True,
    )

    assert resumed is not None
    assert resumed.status == "running"
    assert resumed.end_reason is None
    assert resumed.final_output is None
    assert resumed.usage is None
    assert resumed.error is None
    assert resumed.execution_data == run.execution_data
