"""Fork 的事务、历史边界与运行时隔离契约。"""

import asyncio
import threading
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from test_task5_acceptance import canonical_store as canonical_store

from app.api.schemas import ForkTaskRequest
from app.api.tasks_api import fork_task
from app.models.conversation_run_progress import ConversationRunProgress
from app.models.errors.task_fork_errors import TaskForkConflictError
from app.storage.queries.task_read_queries import TaskReadQueries
from app.task_runtime.service.task_service import TaskService
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.task_runtime.workspace_operation_registry import workspace_operations


@pytest.fixture
def fork_store(request, monkeypatch):
    """复用真实 SQLite 夹具，并禁止 Fork 接触任何 Task runtime space。"""
    store = request.getfixturevalue("canonical_store")
    service = TaskService.__new__(TaskService)
    service._task = store.tasks
    service._turn = store.runs
    service._context = store.context
    service._session_factory = store.factory
    service._task_reads = TaskReadQueries(store.factory)
    store.service = service
    store.context.create_system_prompt(store.task.id, {"role": "system", "content": "固定系统指令"})

    def runtime_access_forbidden(*args, **kwargs):
        raise AssertionError("fork must not access runtime spaces")

    monkeypatch.setattr(task_runtime_spaces, "get_or_create", runtime_access_forbidden)
    yield store


def append_run(store, status):
    run = store.runs.create(store.task.id, status=status)
    sequence = store.context.max_sequence(store.task.id) + 1
    store.context.append(
        store.task.id, run.id, {"role": "user", "content": f"user-{run.id}"}, sequence
    )
    store.context.append(
        store.task.id, run.id, {"role": "assistant", "content": f"assistant-{run.id}"}, sequence + 1
    )
    return run


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("later_status", ["pending", "running", "waiting_for_input"])
def test_fork_terminal_history_while_later_run_is_active(fork_store, terminal, later_status):
    store = fork_store
    first = append_run(store, "completed")
    boundary = append_run(store, terminal)
    later = append_run(store, later_status)
    source_rows = store.contexts.get(store.task.id, include_in_context=False)
    target = asyncio.run(store.service.fork_task(store.task.id, boundary.id))
    cloned_runs = store.runs.list_by_task(target.id)
    assert [run.status for run in cloned_runs] == ["completed", terminal]
    assert target.task_type == "fork"
    assert target.current_run_id is None
    assert target.tool_schemas == store.task.tool_schemas
    assert target.extra == {"fork": {"source_task_id": store.task.id, "source_run_id": boundary.id}}
    assert {run.id for run in cloned_runs}.isdisjoint({first.id, boundary.id, later.id})
    assert all(run.execution_data == ConversationRunProgress() for run in cloned_runs)
    target_rows = store.contexts.get(target.id, include_in_context=False)
    assert [row.message for row in target_rows] == [
        row.message for row in source_rows if row.run_id in {None, first.id, boundary.id}
    ]
    assert [row.sequence for row in target_rows] == list(range(5))
    assert {row.run_id for row in target_rows} == {None, *(run.id for run in cloned_runs)}
    assert store.contexts.get(store.task.id, include_in_context=False) == source_rows
    assert store.runs.get(later.id).status == later_status
    assert task_runtime_spaces.existing_spaces() == ()


@pytest.mark.parametrize("status", ["pending", "running", "waiting_for_input"])
@pytest.mark.parametrize("boundary_is_active", [True, False])
def test_reject_active_run_anywhere_in_selected_prefix(fork_store, status, boundary_is_active):
    store = fork_store
    active = append_run(store, status)
    boundary = active if boundary_is_active else append_run(store, "completed")
    with pytest.raises(TaskForkConflictError) as caught:
        asyncio.run(store.service.fork_task(store.task.id, boundary.id))
    assert caught.value.code == "RUN_NOT_READY"
    assert len(store.service.list_tasks_for_workspace(store.workspace.id)) == 1


def test_unknown_or_foreign_boundary_returns_404_without_creating_task(fork_store):
    store = fork_store
    other = store.tasks.create(store.workspace.id, "other")
    foreign = store.runs.create(other.id, status="completed")
    for boundary in (foreign.id, foreign.id + 100):
        with pytest.raises(HTTPException) as caught:
            asyncio.run(fork_task(store.task.id, ForkTaskRequest(runId=boundary), store.service))
        assert caught.value.status_code == 404
    assert len(store.service.list_tasks_for_workspace(store.workspace.id)) == 2


def test_copy_failure_rolls_back_target_and_runs(fork_store, monkeypatch):
    store = fork_store
    run = append_run(store, "completed")

    def fail_copy(*args):
        raise RuntimeError("copy failed")

    monkeypatch.setattr(store.context, "clone_for_fork", fail_copy)
    with pytest.raises(RuntimeError, match="copy failed"):
        asyncio.run(store.service.fork_task(store.task.id, run.id))
    assert len(store.service.list_tasks_for_workspace(store.workspace.id)) == 1
    assert len(store.runs.list_by_task(store.task.id)) == 1


def test_workspace_busy_is_mapped_to_retryable_409(fork_store, monkeypatch):
    store = fork_store
    run = append_run(store, "completed")

    @contextmanager
    def busy(workspace_id, timeout):
        assert workspace_id == store.workspace.id
        assert timeout == 10
        raise TimeoutError("workspace busy")
        yield  # pragma: no cover

    monkeypatch.setattr(workspace_operations, "operation", busy)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(fork_task(store.task.id, ForkTaskRequest(runId=run.id), store.service))
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "WORKSPACE_BUSY"
    assert caught.value.detail["retryable"] is True


def test_success_response_does_not_read_database_after_commit(fork_store, monkeypatch):
    store = fork_store
    run = append_run(store, "completed")

    def forbidden(*args):
        raise AssertionError("post-commit read")

    monkeypatch.setattr(store.service, "get_task", forbidden)
    response = asyncio.run(fork_task(store.task.id, ForkTaskRequest(runId=run.id), store.service))
    assert response.task_id != store.task.id
    assert "fork_available" not in response.model_dump()


def test_request_cancellation_does_not_release_workspace_before_worker_finishes(
    fork_store, monkeypatch
):
    store = fork_store
    run = append_run(store, "completed")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_copy = store.context.clone_for_fork
    original_worker = store.service._fork_task_in_workspace

    def slow_copy(*args):
        entered.set()
        assert release.wait(3)
        return original_copy(*args)

    def worker(*args):
        try:
            return original_worker(*args)
        finally:
            finished.set()

    monkeypatch.setattr(store.context, "clone_for_fork", slow_copy)
    monkeypatch.setattr(store.service, "_fork_task_in_workspace", worker)

    async def scenario():
        request = asyncio.create_task(store.service.fork_task(store.task.id, run.id))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            with (
                pytest.raises(TimeoutError),
                workspace_operations.operation(store.workspace.id, timeout=0),
            ):
                pass
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 3)
        with workspace_operations.operation(store.workspace.id, timeout=0):
            assert len(store.service.list_tasks_for_workspace(store.workspace.id)) == 2

    asyncio.run(scenario())
