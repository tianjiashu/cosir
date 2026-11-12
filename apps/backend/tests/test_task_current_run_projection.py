"""Task 状态的读取投影：执行状态只认当前 Run 指针，等你操作只认等待占位行；Task 读取接口不携带状态。"""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.api.tasks_api import get_task
from app.api.workspaces_api import list_workspace_tasks
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.message_metadata import BackgroundWork, ToolMessageMetadata
from app.models.task_extra import TaskExtra
from app.service.task.task_service import TaskService
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.engine_cache import create_sqlite_engine
from app.storage.model.conversation_task_context_model import ConversationTaskContextModel
from app.storage.queries.task_status_queries import TaskStatus, TaskStatusQueries
from app.storage.store_engines import initialize_app_schema
from tests.task_extra_fixtures import fork_extra


@pytest.fixture
def store(tmp_path):
    engine = create_sqlite_engine(tmp_path / "app.sqlite3")
    initialize_app_schema(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    cruds = []
    for cls in (TaskCrud, ConversationRunCrud, WorkspaceCrud, ConversationTaskContextCrud):
        crud = cls.__new__(cls)
        crud._session_factory = factory
        cruds.append(crud)
    tasks, runs, workspaces, contexts = cruds
    workspace = workspaces.create("投影验收", str(tmp_path))
    service = TaskService.__new__(TaskService)
    service._task = tasks
    yield SimpleNamespace(
        tasks=tasks, runs=runs, contexts=contexts, workspace=workspace,
        statuses=TaskStatusQueries(factory), service=service, factory=factory,
    )
    engine.dispose()


@pytest.mark.parametrize("status", ["running", "suspended", "completed", "failed", "cancelled"])
def test_status_follows_the_current_pointer_not_the_latest_run(store, status):
    task = store.tasks.create(store.workspace.id, "子任务", task_type="agent_task")
    current = store.runs.create(task.id, status=status)
    store.runs.create(task.id, status="completed")
    store.tasks.set_current_run_id(task.id, current.id)
    assert store.statuses.status(task.id) == TaskStatus(status, 0)
    assert store.statuses.list_statuses() == {task.id: TaskStatus(status, 0)}


def test_no_run_and_missing_task(store):
    task = store.tasks.create(store.workspace.id, "未运行")
    assert store.statuses.status(task.id) == TaskStatus(None, 0)
    with pytest.raises(KeyError):
        store.statuses.status(task.id + 100)


def test_status_is_read_fresh(store):
    task = store.tasks.create(store.workspace.id, "恢复")
    run = store.runs.create(task.id, status="cancelled")
    store.tasks.set_current_run_id(task.id, run.id)
    assert store.statuses.status(task.id).execution_status == "cancelled"
    store.runs.update_status_if_in(run.id, "running", ("cancelled",))
    assert store.statuses.status(task.id).execution_status == "running"


def _tool_row(task_id, sequence, metadata, *, run_id, include=True):
    return ConversationTaskContextRecord(
        task_id=task_id,
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
    task = store.tasks.create(store.workspace.id, "审批")
    run = store.runs.create(task.id, status="completed")
    store.tasks.set_current_run_id(task.id, run.id)
    background = BackgroundWork(
        kind="terminal_command", run_id=run.id, label="build", started_at="2026-11-12T00:00:00Z"
    )
    rows = [
        _tool_row(task.id, 1, ToolMessageMetadata(status="pending"), run_id=run.id),
        _tool_row(task.id, 2, ToolMessageMetadata(status="pending"), run_id=run.id),
        _tool_row(task.id, 3, ToolMessageMetadata(status="pending", background=background), run_id=run.id),
        _tool_row(task.id, 4, ToolMessageMetadata(status="running"), run_id=run.id),
        _tool_row(task.id, 5, ToolMessageMetadata(status="pending"), run_id=None),
        _tool_row(task.id, 6, ToolMessageMetadata(status="pending"), run_id=run.id, include=False),
        _tool_row(task.id, 7, ToolMessageMetadata(status="completed"), run_id=run.id),
    ]
    for row in rows:
        store.contexts.create(row)

    assert [row.awaits_user for row in rows] == [True, True, False, False, False, False, False]
    assert store.statuses.status(task.id) == TaskStatus("completed", 2)
    assert store.statuses.list_statuses() == {task.id: TaskStatus("completed", 2)}


def test_awaiting_user_queries_use_the_partial_index(store):
    """整表对账每 15 秒一次、每次工具状态迁移按 Task 读一次：两条计数都必须走只收录 pending 行的
    部分索引，不扫整张 context 表，也不扫这个 Task 的全部行。"""
    queries = TaskStatusQueries._AWAITING, TaskStatusQueries._AWAITING.where(
        ConversationTaskContextModel.task_id == 1
    )
    with store.factory() as session:
        for statement in queries:
            sql = str(statement.compile(session.get_bind(), compile_kwargs={"literal_binds": True}))
            plan = session.execute(text(f"EXPLAIN QUERY PLAN {sql}")).all()
            assert "idx_task_context_awaiting" in str(plan), plan


def test_task_responses_carry_no_execution_state(store):
    for kind in ("user", "user_fork", "agent_fork", "agent_task"):
        task = store.tasks.create(store.workspace.id, kind, task_type=kind,
                                  extra=fork_extra(987) if kind.endswith("_fork") else TaskExtra())
        run = store.runs.create(task.id, status="suspended")
        store.tasks.set_current_run_id(task.id, run.id)
    records = asyncio.run(list_workspace_tasks(store.workspace.id, store.service))
    assert {record.task_type for record in records} == {"user", "user_fork", "agent_fork"}
    # 当前 Run 指针是后端内部事实；执行状态只经状态推送下发。二者都不进 Task 响应。
    for response in [*records, asyncio.run(get_task(task.id, store.service))]:
        dumped = response.model_dump()
        assert "current_run_id" not in dumped and "execution_status" not in dumped
