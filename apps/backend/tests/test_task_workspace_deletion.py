import threading
from pathlib import Path

import pytest
from fastapi import HTTPException
from langchain_core.messages import HumanMessage
from sqlalchemy import func, select

from app.api.tasks_api import delete_task as delete_task_endpoint
from app.api.workspaces_api import delete_workspace as delete_workspace_endpoint
from app.config.settings import Settings
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.errors.deletion_errors import DeletionBusyError
from app.service.depends import (
    close_service_dependencies,
    get_conversation_run_crud,
    get_conversation_task_context_crud,
    get_task_crud,
    get_task_service,
    get_workspace_crud,
    get_workspace_service,
)
from app.storage.model.conversation_run_model import ConversationRunModel
from app.storage.model.conversation_task_context_model import ConversationTaskContextModel
from app.storage.model.task_model import TaskModel
from app.storage.model.workspace_model import WorkspaceModel
from app.storage.store_engines import init_storage, main_session_factory
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


@pytest.fixture
def storage(tmp_path: Path):
    """为删除测试提供隔离的主库和 checkpoint 路径。"""

    close_service_dependencies()
    db_dir = tmp_path / "storage"
    db_dir.mkdir()
    Settings.override(
        DATABASE_FILE=db_dir / "app.sqlite3",
        LOG_DATABASE_FILE=db_dir / "log.sqlite3",
        CHECKPOINT_FILE=db_dir / "checkpoints.sqlite3",
        LOG_DIR=db_dir / "logs",
    )
    init_storage()
    task_runtime_spaces.close()
    yield
    task_runtime_spaces.close()
    close_service_dependencies()


def _new_workspace_and_task():
    workspace = get_workspace_crud().create("review", str(Path.cwd()))
    task = get_task_crud().create(workspace.id, "root")
    return workspace, task


def _count(model: type[object]) -> int:
    with main_session_factory()() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_delete_task_removes_nested_tasks_and_runs(storage) -> None:
    workspace, root = _new_workspace_and_task()
    child = get_task_crud().create(
        workspace.id,
        "child",
        task_type="delegation",
        parent_task_id=root.id,
    )
    root_run = get_conversation_run_crud().create(root.id, "root input", status="completed")
    child_run = get_conversation_run_crud().create(child.id, "child input", status="failed")
    context_crud = get_conversation_task_context_crud()
    context_crud.create(
        ConversationTaskContextRecord(
            task_id=root.id,
            run_id=root_run.id,
            message=HumanMessage(content="root canonical message"),
            include_in_context=True,
            sequence=1,
        )
    )
    context_crud.create(
        ConversationTaskContextRecord(
            task_id=child.id,
            run_id=child_run.id,
            message=HumanMessage(content="child canonical message"),
            include_in_context=True,
            sequence=1,
        )
    )

    get_task_service().delete_task(root.id)

    assert _count(TaskModel) == 0
    assert _count(ConversationRunModel) == 0
    assert _count(ConversationTaskContextModel) == 0
    assert _count(WorkspaceModel) == 1


def test_delete_missing_task_does_not_create_runtime_space(storage) -> None:
    missing_id = 987654

    with pytest.raises(KeyError):
        get_task_service().delete_task(missing_id)

    assert task_runtime_spaces.get(missing_id) is None


def test_task_delete_rolls_back_all_rows_when_tree_delete_fails(
    storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, root = _new_workspace_and_task()
    child = get_task_crud().create(
        workspace.id,
        "child",
        task_type="delegation",
        parent_task_id=root.id,
    )
    get_conversation_run_crud().create(root.id, "root input", status="completed")
    get_conversation_run_crud().create(child.id, "child input", status="completed")
    service = get_task_service()
    original_delete = service._delete_single_task_in_session
    calls: list[int] = []

    def delete_with_failure(task_id: int, session):
        calls.append(task_id)
        if len(calls) == 2:
            raise RuntimeError("injected task deletion failure")
        return original_delete(task_id, session)

    monkeypatch.setattr(service, "_delete_single_task_in_session", delete_with_failure)

    with pytest.raises(RuntimeError, match="injected"):
        service.delete_task(root.id)

    assert calls == [child.id, root.id]
    assert set(get_task_crud().list_ids_by_workspace(workspace.id)) == {
        root.id,
        child.id,
    }
    assert _count(ConversationRunModel) == 2


def test_task_delete_recollects_children_inside_write_transaction(
    storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, root = _new_workspace_and_task()
    service = get_task_service()
    collected = threading.Event()
    release = threading.Event()
    original_collect = service._collect_task_tree_ids
    first_call = True

    def collect_then_pause(task_id: int, session=None) -> set[int]:
        nonlocal first_call
        result = original_collect(task_id, session=session)
        if first_call and session is None:
            first_call = False
            collected.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(service, "_collect_task_tree_ids", collect_then_pause)
    created: list[int] = []

    def create_child() -> None:
        child = service.get_or_create_task(
            workspace.id,
            "created-between-tree-read-and-transaction",
            task_type="delegation",
            parent_task_id=root.id,
        )
        created.append(child.id)

    delete_thread = threading.Thread(target=service.delete_task, args=(root.id,))
    create_thread = threading.Thread(target=create_child)
    delete_thread.start()
    assert collected.wait(5)
    create_thread.start()
    create_thread.join(5)
    release.set()
    delete_thread.join(10)

    assert created
    assert not delete_thread.is_alive()
    assert get_task_crud().list_ids_by_workspace(workspace.id) == []


def test_workspace_delete_rolls_back_all_rows_when_task_delete_fails(
    storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = get_workspace_crud().create("review", str(Path.cwd()))
    root_ids = [
        get_task_crud().create(workspace.id, "root-1").id,
        get_task_crud().create(workspace.id, "root-2").id,
    ]
    service = get_workspace_service()
    original_delete = service._task_service.delete_task_tree_in_session
    calls: list[int] = []

    def delete_with_failure(task_id: int, session):
        calls.append(task_id)
        if len(calls) == 2:
            raise RuntimeError("injected root deletion failure")
        return original_delete(task_id, session)

    monkeypatch.setattr(service._task_service, "delete_task_tree_in_session", delete_with_failure)

    with pytest.raises(RuntimeError, match="injected"):
        service.delete_workspace(workspace.id)

    assert calls == root_ids
    assert get_workspace_crud().get(workspace.id).id == workspace.id
    assert set(get_task_crud().list_ids_by_workspace(workspace.id)) == set(root_ids)


def test_workspace_delete_serializes_task_creation(
    storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, root = _new_workspace_and_task()
    service = get_workspace_service()
    listed = threading.Event()
    release = threading.Event()
    original_list = service._task_crud.list_ids_by_workspace
    first_call = True

    def list_then_pause(workspace_id: int, session=None) -> list[int]:
        nonlocal first_call
        result = original_list(workspace_id, session=session)
        if first_call and session is None:
            first_call = False
            listed.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(service._task_crud, "list_ids_by_workspace", list_then_pause)
    deletion_error: list[BaseException] = []
    creation_error: list[BaseException] = []

    def delete() -> None:
        try:
            service.delete_workspace(workspace.id)
        except BaseException as exc:
            deletion_error.append(exc)

    def create() -> None:
        try:
            service.create_task(workspace.id, "created-during-delete")
        except BaseException as exc:
            creation_error.append(exc)

    delete_thread = threading.Thread(target=delete)
    create_thread = threading.Thread(target=create)
    delete_thread.start()
    assert listed.wait(5)
    create_thread.start()
    release.set()
    delete_thread.join(10)
    create_thread.join(10)

    assert deletion_error == []
    assert len(creation_error) == 1
    assert isinstance(creation_error[0], KeyError)
    with pytest.raises(KeyError):
        get_workspace_crud().get(workspace.id)
    assert get_task_crud().list_ids_by_workspace(workspace.id) == []
    assert root.id > 0


@pytest.mark.asyncio
async def test_delete_endpoints_return_404_and_busy_409(storage) -> None:
    class MissingTask:
        def delete_task(self, _task_id: int) -> None:
            raise KeyError(_task_id)

    class MissingWorkspace:
        def delete_workspace(self, _workspace_id: int) -> None:
            raise KeyError(_workspace_id)

    with pytest.raises(HTTPException) as task_404:
        await delete_task_endpoint(1, MissingTask())
    with pytest.raises(HTTPException) as workspace_404:
        await delete_workspace_endpoint(1, MissingWorkspace())
    assert task_404.value.status_code == 404
    assert workspace_404.value.status_code == 404

    class BusyTask:
        def delete_task(self, _task_id: int) -> None:
            raise DeletionBusyError("task", _task_id)

    class BusyWorkspace:
        def delete_workspace(self, _workspace_id: int) -> None:
            raise DeletionBusyError("workspace", _workspace_id)

    with pytest.raises(HTTPException) as task_409:
        await delete_task_endpoint(1, BusyTask())
    with pytest.raises(HTTPException) as workspace_409:
        await delete_workspace_endpoint(1, BusyWorkspace())
    assert task_409.value.status_code == 409
    assert task_409.value.detail["code"] == "TASK_BUSY"
    assert workspace_409.value.status_code == 409
    assert workspace_409.value.detail["code"] == "WORKSPACE_BUSY"
