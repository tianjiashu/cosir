"""Task 当前 Run 读取投影的事实来源、批量查询与 API 契约。"""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from app.api.schemas.response.TaskResponse import TaskResponse
from app.api.tasks_api import get_task
from app.api.workspaces_api import list_workspace_tasks
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.engine_cache import create_sqlite_engine
from app.storage.queries.task_read_queries import TaskReadQueries
from app.storage.store_engines import initialize_app_schema
from app.task_runtime.service.task_service import TaskService


@pytest.fixture
def store(tmp_path):
    engine = create_sqlite_engine(tmp_path / "app.sqlite3")
    initialize_app_schema(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    cruds = []
    for cls in (TaskCrud, ConversationRunCrud, WorkspaceCrud):
        crud = cls.__new__(cls)
        crud._session_factory = factory
        cruds.append(crud)
    tasks, runs, workspaces = cruds
    workspace = workspaces.create("投影验收", str(tmp_path))
    reads = TaskReadQueries(factory)
    service = TaskService.__new__(TaskService)
    service._task_reads = reads
    yield SimpleNamespace(
        engine=engine, tasks=tasks, runs=runs, workspace=workspace, reads=reads, service=service
    )
    engine.dispose()


@pytest.mark.parametrize(
    "status", ["running", "suspended", "completed", "failed", "cancelled"]
)
def test_uses_current_pointer_instead_of_latest_created_run(store, status):
    task = store.tasks.create(store.workspace.id, "子任务", task_type="delegate_task")
    current = store.runs.create(task.id, status=status)
    store.runs.create(task.id, status="completed")
    store.tasks.set_current_run_id(task.id, current.id)
    record = store.service.get_task(task.id)
    assert (record.current_run_id, record.execution_status) == (current.id, status)
    response = asyncio.run(get_task(task.id, store.service))
    assert response.execution_status == status
    # 当前 Run 指针是后端内部事实，不进 HTTP 响应。
    assert "current_run_id" not in response.model_dump()


def test_no_run_and_missing_task(store):
    task = store.tasks.create(store.workspace.id, "未运行")
    response = TaskResponse.from_record(store.reads.get(task.id))
    assert response.execution_status is None
    with pytest.raises(KeyError):
        store.service.get_task(task.id + 100)


def test_resume_and_replacement_are_read_without_cached_state(store):
    task = store.tasks.create(store.workspace.id, "恢复")
    run = store.runs.create(task.id, status="cancelled")
    store.tasks.set_current_run_id(task.id, run.id)
    assert store.reads.get(task.id).execution_status == "cancelled"
    store.runs.update_status_if_in(run.id, "running", ("cancelled",))
    assert store.reads.get(task.id).execution_status == "running"
    later = store.runs.create(task.id, status="running")
    store.tasks.set_current_run_id(task.id, later.id)
    assert store.reads.get(task.id).current_run_id == later.id


def test_workspace_projection_is_one_query_and_keeps_catalog_filter(store):
    for kind in ("user", "fork", "delegate_task"):
        task = store.tasks.create(store.workspace.id, kind, task_type=kind)
        run = store.runs.create(task.id, status="suspended")
        store.tasks.set_current_run_id(task.id, run.id)
    statements = []

    def record_query(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(store.engine, "before_cursor_execute", record_query)
    try:
        records = asyncio.run(list_workspace_tasks(store.workspace.id, store.service))
    finally:
        event.remove(store.engine, "before_cursor_execute", record_query)
    assert len(statements) == 1
    assert {record.task_type for record in records} == {"user", "fork"}
    assert all(record.execution_status == "suspended" for record in records)
