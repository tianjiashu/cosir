"""Task 执行状态的读取投影：只认当前 Run 指针，Task 读取接口不再携带状态。"""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from app.api.tasks_api import get_task
from app.api.workspaces_api import list_workspace_tasks
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.engine_cache import create_sqlite_engine
from app.storage.queries.task_status_queries import TaskStatusQueries
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
    service = TaskService.__new__(TaskService)
    service._task = tasks
    yield SimpleNamespace(
        tasks=tasks, runs=runs, workspace=workspace, statuses=TaskStatusQueries(factory), service=service
    )
    engine.dispose()


@pytest.mark.parametrize("status", ["running", "suspended", "completed", "failed", "cancelled"])
def test_status_follows_the_current_pointer_not_the_latest_run(store, status):
    task = store.tasks.create(store.workspace.id, "子任务", task_type="delegate_task")
    current = store.runs.create(task.id, status=status)
    store.runs.create(task.id, status="completed")
    store.tasks.set_current_run_id(task.id, current.id)
    assert store.statuses.status(task.id) == status
    assert store.statuses.list_statuses() == {task.id: status}


def test_no_run_and_missing_task(store):
    task = store.tasks.create(store.workspace.id, "未运行")
    assert store.statuses.status(task.id) is None
    with pytest.raises(KeyError):
        store.statuses.status(task.id + 100)


def test_status_is_read_fresh(store):
    task = store.tasks.create(store.workspace.id, "恢复")
    run = store.runs.create(task.id, status="cancelled")
    store.tasks.set_current_run_id(task.id, run.id)
    assert store.statuses.status(task.id) == "cancelled"
    store.runs.update_status_if_in(run.id, "running", ("cancelled",))
    assert store.statuses.status(task.id) == "running"


def test_task_responses_carry_no_execution_state(store):
    for kind in ("user", "fork", "delegate_task"):
        task = store.tasks.create(store.workspace.id, kind, task_type=kind)
        run = store.runs.create(task.id, status="suspended")
        store.tasks.set_current_run_id(task.id, run.id)
    records = asyncio.run(list_workspace_tasks(store.workspace.id, store.service))
    assert {record.task_type for record in records} == {"user", "fork"}
    # 当前 Run 指针是后端内部事实；执行状态只经状态推送下发。二者都不进 Task 响应。
    for response in [*records, asyncio.run(get_task(task.id, store.service))]:
        dumped = response.model_dump()
        assert "current_run_id" not in dumped and "execution_status" not in dumped
