"""Session 状态的读取投影：执行状态只认当前 Run 指针，等你操作只认等待占位行；Session 读取接口不携带状态。"""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.api.sessions_api import get_session
from app.api.workspaces_api import list_workspace_sessions
from app.models.session_context import SessionContextRecord
from app.models.message_metadata import BackgroundWork, ToolMessageMetadata
from app.models.session_extra import SessionExtra
from app.service.session.session_service import SessionService
from app.storage.crud.run_crud import RunCrud
from app.storage.crud.session_context_crud import SessionContextCrud
from app.storage.crud.session_crud import SessionCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.engine_cache import create_sqlite_engine
from app.storage.model.session_context_model import SessionContextModel
from app.storage.queries.session_status_queries import SessionStatus, SessionStatusQueries
from app.storage.store_engines import initialize_app_schema
from tests.session_extra_fixtures import fork_extra


@pytest.fixture
def store(tmp_path):
    engine = create_sqlite_engine(tmp_path / "app.sqlite3")
    initialize_app_schema(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    cruds = []
    for cls in (SessionCrud, RunCrud, WorkspaceCrud, SessionContextCrud):
        crud = cls.__new__(cls)
        crud._db_factory = factory
        cruds.append(crud)
    sessions, runs, workspaces, contexts = cruds
    workspace = workspaces.create("投影验收", str(tmp_path))
    service = SessionService.__new__(SessionService)
    service._session = sessions
    yield SimpleNamespace(
        sessions=sessions, runs=runs, contexts=contexts, workspace=workspace,
        statuses=SessionStatusQueries(factory), service=service, factory=factory,
    )
    engine.dispose()


@pytest.mark.parametrize("status", ["running", "suspended", "completed", "failed", "cancelled"])
def test_status_follows_the_current_pointer_not_the_latest_run(store, status):
    session = store.sessions.create(store.workspace.id, "子会话", session_type="delegated")
    current = store.runs.create(session.id, status=status)
    store.runs.create(session.id, status="completed")
    store.sessions.set_current_run_id(session.id, current.id)
    assert store.statuses.status(session.id) == SessionStatus(status, 0)
    assert store.statuses.list_statuses() == {session.id: SessionStatus(status, 0)}


def test_no_run_and_missing_session(store):
    session = store.sessions.create(store.workspace.id, "未运行")
    assert store.statuses.status(session.id) == SessionStatus(None, 0)
    with pytest.raises(KeyError):
        store.statuses.status(session.id + 100)


def test_status_is_read_fresh(store):
    session = store.sessions.create(store.workspace.id, "恢复")
    run = store.runs.create(session.id, status="cancelled")
    store.sessions.set_current_run_id(session.id, run.id)
    assert store.statuses.status(session.id).execution_status == "cancelled"
    store.runs.update_status_if_in(run.id, "running", ("cancelled",))
    assert store.statuses.status(session.id).execution_status == "running"


def _tool_row(session_id, sequence, metadata, *, run_id, include=True):
    return SessionContextRecord(
        session_id=session_id,
        run_id=run_id,
        message={"role": "tool", "tool_call_id": f"call-{sequence}", "content": "x"},
        include_in_context=include,
        sequence=sequence,
        turn=1,
        metadata=metadata,
    )


def test_awaiting_user_counts_only_local_hitl_placeholders_still_in_context(store):
    """等你操作与 Run 状态正交：Run 已完成也照常计数；后台占位、已接收的决定、继承副本与撤回移出的
    调用都不算。"""
    session = store.sessions.create(store.workspace.id, "审批")
    run = store.runs.create(session.id, status="completed")
    store.sessions.set_current_run_id(session.id, run.id)
    background = BackgroundWork(
        kind="terminal_command", run_id=run.id, label="build", started_at="2026-11-12T00:00:00Z"
    )
    rows = [
        _tool_row(session.id, 1, ToolMessageMetadata(status="pending"), run_id=run.id),
        _tool_row(session.id, 2, ToolMessageMetadata(status="pending"), run_id=run.id),
        _tool_row(session.id, 3, ToolMessageMetadata(status="pending", background=background), run_id=run.id),
        _tool_row(session.id, 4, ToolMessageMetadata(status="running"), run_id=run.id),
        _tool_row(session.id, 5, ToolMessageMetadata(status="pending"), run_id=None),
        _tool_row(session.id, 6, ToolMessageMetadata(status="pending"), run_id=run.id, include=False),
        _tool_row(session.id, 7, ToolMessageMetadata(status="completed"), run_id=run.id),
    ]
    for row in rows:
        store.contexts.create(row)

    assert [row.awaits_user for row in rows] == [True, True, False, False, False, False, False]
    assert store.statuses.status(session.id) == SessionStatus("completed", 2)
    assert store.statuses.list_statuses() == {session.id: SessionStatus("completed", 2)}


def test_awaiting_user_queries_use_the_partial_index(store):
    """整表对账每 15 秒一次、每次工具状态迁移按 Session 读一次：两条计数都必须走只收录 pending 行的
    部分索引，不扫整张 context 表，也不扫这个 Session 的全部行。"""
    queries = SessionStatusQueries._AWAITING, SessionStatusQueries._AWAITING.where(
        SessionContextModel.session_id == 1
    )
    with store.factory() as db:
        for statement in queries:
            sql = str(statement.compile(db.get_bind(), compile_kwargs={"literal_binds": True}))
            plan = db.execute(text(f"EXPLAIN QUERY PLAN {sql}")).all()
            assert "idx_session_context_awaiting" in str(plan), plan


def test_session_responses_carry_no_execution_state(store):
    for kind in ("user", "user_fork", "agent_fork", "delegated"):
        session = store.sessions.create(store.workspace.id, kind, session_type=kind,
                                  extra=fork_extra(987) if kind.endswith("_fork") else SessionExtra())
        run = store.runs.create(session.id, status="suspended")
        store.sessions.set_current_run_id(session.id, run.id)
    records = asyncio.run(list_workspace_sessions(store.workspace.id, store.service))
    assert {record.session_type for record in records} == {"user", "user_fork", "agent_fork"}
    # 当前 Run 指针是后端内部事实；执行状态只经状态推送下发。二者都不进 Session 响应。
    for response in [*records, asyncio.run(get_session(session.id, store.service))]:
        dumped = response.model_dump()
        assert "current_run_id" not in dumped and "execution_status" not in dumped
