import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.tasks_api import delete_task as delete_task_endpoint
from app.api.workspaces_api import delete_workspace as delete_workspace_endpoint
from app.models.conversation_input import UserInputPresentation
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.errors.deletion_errors import DeletionBusyError
from app.models.message_metadata import UserMessageMetadata
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
from app.utils.path import system_cosir as paths


def _user_meta(text: str) -> UserMessageMetadata:
    """落库的用户消息都带来源 ``kind``；用户输入还带展示绑定（snapshot 重建与 fork 只从这里取）。"""
    return UserMessageMetadata(
        kind="user_input", presentation=UserInputPresentation(display_text=text)
    )


@pytest.fixture
def storage(tmp_path: Path, install_process_singletons):
    """为删除测试提供隔离的主库和 checkpoint 路径，并装配进程级单例。

    删除链路会经工具系统与 agent 目录做收敛（未装配即 ``RuntimeError``）；单例必须在主库初始化
    之后安装，故经 ``install_process_singletons()`` 显式调用。
    """

    close_service_dependencies()
    db_dir = tmp_path / "storage"
    db_dir.mkdir()
    paths.override(
        DATABASE_FILE=db_dir / "app.sqlite3",
        LOG_DIR=db_dir / "logs",
    )
    init_storage()
    install_process_singletons()
    task_runtime_spaces.close()
    yield
    task_runtime_spaces.close()
    close_service_dependencies()
    paths.reset()


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
        task_type="delegate_task",
        parent_task_id=root.id,
    )
    # Team 节点与委派子 Task 同样挂在 parent_task_id 下；嵌套一层验证后序删除。
    team_node = get_task_crud().create(
        workspace.id,
        "team node",
        task_type="agent_team_node",
        parent_task_id=root.id,
    )
    nested = get_task_crud().create(
        workspace.id,
        "nested",
        task_type="delegate_task",
        parent_task_id=team_node.id,
    )
    get_conversation_run_crud().create(team_node.id, status="completed")
    get_conversation_run_crud().create(nested.id, status="completed")
    root_run = get_conversation_run_crud().create(root.id, status="completed")
    child_run = get_conversation_run_crud().create(child.id, status="failed")
    context_crud = get_conversation_task_context_crud()
    context_crud.create(
        ConversationTaskContextRecord(
            task_id=root.id,
            run_id=root_run.id,
            message={"role": "user", "content": "root canonical message"},
            metadata=_user_meta("root canonical message"),
            include_in_context=True,
            sequence=1,
        )
    )
    context_crud.create(
        ConversationTaskContextRecord(
            task_id=child.id,
            run_id=child_run.id,
            message={"role": "user", "content": "child canonical message"},
            metadata=_user_meta("child canonical message"),
            include_in_context=True,
            sequence=1,
        )
    )

    get_task_service().delete_task(root.id)

    assert _count(TaskModel) == 0
    assert _count(ConversationRunModel) == 0
    assert _count(ConversationTaskContextModel) == 0
    assert _count(WorkspaceModel) == 1


def test_task_and_workspace_deletion_preserve_workspace_attachments(
    storage, tmp_path: Path
) -> None:
    """删除 Task 或 Workspace 业务事实时，workspace 图片文件保持永久保留。"""

    root = tmp_path / "workspace-with-attachments"
    root.mkdir()
    workspace = get_workspace_crud().create("attachments", str(root))
    task = get_task_crud().create(workspace.id, "root")
    attachment_dir = root / ".cosir" / "Attachment"
    attachment_dir.mkdir(parents=True)
    attachment = attachment_dir / ("a" * 64 + ".png")
    attachment.write_bytes(b"published image")

    get_task_service().delete_task(task.id)
    assert attachment.exists()

    get_workspace_service().delete_workspace(workspace.id)
    assert attachment.exists()


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
        task_type="delegate_task",
        parent_task_id=root.id,
    )
    get_conversation_run_crud().create(root.id, status="completed")
    get_conversation_run_crud().create(child.id, status="completed")
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

    def collect_then_pause(task_id: int) -> set[int]:
        nonlocal first_call
        result = original_collect(task_id)
        if first_call:
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
            task_type="delegate_task",
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
async def test_delete_endpoints_return_404_and_busy_409() -> None:
    def failing(error: Exception):
        async def fail(_id: int, **_options: object) -> None:
            raise error

        return fail

    class FakeTasks:
        def __init__(self, error: Exception) -> None:
            self.delete_task_tree = failing(error)

    class FakeWorkspaces:
        def __init__(self, error: Exception) -> None:
            self.delete_workspace_tree = failing(error)

    with pytest.raises(HTTPException) as task_404:
        await delete_task_endpoint(1, FakeTasks(KeyError(1)), None, False)
    with pytest.raises(HTTPException) as workspace_404:
        await delete_workspace_endpoint(1, FakeWorkspaces(KeyError(1)))
    assert task_404.value.status_code == 404
    assert workspace_404.value.status_code == 404

    with pytest.raises(HTTPException) as task_409:
        await delete_task_endpoint(1, FakeTasks(DeletionBusyError("task", 1)), None, False)
    with pytest.raises(HTTPException) as workspace_409:
        await delete_workspace_endpoint(1, FakeWorkspaces(DeletionBusyError("workspace", 1)))
    assert task_409.value.status_code == 409
    assert task_409.value.detail["code"] == "TASK_BUSY"
    assert workspace_409.value.status_code == 409
    assert workspace_409.value.detail["code"] == "WORKSPACE_BUSY"


@pytest.mark.asyncio
async def test_delete_task_endpoint_routes_creation_marker_to_guarded_cleanup(storage) -> None:
    calls: list[tuple[int, str]] = []

    class ProvisionalTask:
        def cleanup_provisional_task(self, task_id: int, creation_command_id: str) -> None:
            calls.append((task_id, creation_command_id))

        async def delete_task_tree(self, _task_id: int, **_options: object) -> None:
            raise AssertionError("provisional cleanup must not use ordinary deletion")

    result = await delete_task_endpoint(7, ProvisionalTask(), "creation-7", False)

    assert result.task_id == 7
    assert result.deleted_task_ids == [7]
    assert calls == [(7, "creation-7")]


class _ActorLog:
    """记下删除用例对 actor 的停止与回滚；不触达真实 actor。"""

    def __init__(self) -> None:
        self.stopped: list[set[int]] = []
        self.restarted: list[set[int]] = []

    async def stop(self, task_ids: set[int]) -> None:
        self.stopped.append(set(task_ids))

    def restart_stopped(self, task_ids: set[int]) -> None:
        self.restarted.append(set(task_ids))


@pytest.fixture
def actor_log(monkeypatch) -> _ActorLog:
    log = _ActorLog()
    monkeypatch.setattr(task_runtime_spaces, "stop", log.stop)
    monkeypatch.setattr(task_runtime_spaces, "restart_stopped", log.restart_stopped)
    return log


@pytest.mark.asyncio
async def test_delete_task_tree_stops_until_the_tree_stops_growing(
    storage, monkeypatch, actor_log
) -> None:
    """父 Agent 在停下前刚委派出的子 Task 也要先停：收集与停止反复到子树不再增长。"""
    service = get_task_service()
    trees = iter([{1}, {1, 2}, {1, 2}])
    deleted: list[int] = []
    monkeypatch.setattr(service, "collect_task_tree_ids", lambda _task_id: next(trees))
    monkeypatch.setattr(service, "delete_task", lambda task_id, _fork_ids: deleted.append(task_id))

    await service.delete_task_tree(1)

    assert actor_log.stopped == [{1}, {2}]
    assert deleted == [1]
    assert actor_log.restarted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [DeletionBusyError("task", 1), RuntimeError("db")])
async def test_failed_task_deletion_restarts_the_stopped_actors(
    storage, monkeypatch, actor_log, error
) -> None:
    """删除没提交时 Task 仍在，停下的 actor 不可逆，必须换上新的调度者。"""
    service = get_task_service()
    monkeypatch.setattr(service, "collect_task_tree_ids", lambda _task_id: {1, 2})

    def fail(_task_id: int, _fork_ids: frozenset[int]) -> None:
        raise error

    monkeypatch.setattr(service, "delete_task", fail)

    with pytest.raises(type(error)):
        await service.delete_task_tree(1)

    assert actor_log.stopped == [{1, 2}]
    assert actor_log.restarted == [{1, 2}]


@pytest.mark.asyncio
async def test_failed_workspace_deletion_restarts_the_stopped_actors(
    storage, tmp_path, monkeypatch, actor_log
) -> None:
    workspace = get_workspace_crud().create("busy", str(tmp_path))
    task = get_task_crud().create(workspace.id, "owner")
    service = get_workspace_service()

    def busy(_workspace_id: int) -> None:
        raise DeletionBusyError("workspace", _workspace_id)

    monkeypatch.setattr(service, "delete_workspace", busy)

    with pytest.raises(DeletionBusyError):
        await service.delete_workspace_tree(workspace.id)

    assert actor_log.stopped == [{task.id}]
    assert actor_log.restarted == [{task.id}]


def test_restart_stopped_only_replaces_stopped_actors(monkeypatch) -> None:
    """停止之后才物化的 space 带着正常运行的 actor，换掉会让两个 loop 调度同一个 Task。"""
    restarted: list[int] = []

    def space(task_id: int, *, stopped: bool):
        event = asyncio.Event()
        if stopped:
            event.set()
        return SimpleNamespace(
            actor=SimpleNamespace(stopped=event),
            restart_actor=lambda: restarted.append(task_id),
        )

    spaces = {1: space(1, stopped=True), 2: space(2, stopped=False)}
    monkeypatch.setattr(task_runtime_spaces, "get", spaces.get)

    task_runtime_spaces.restart_stopped({1, 2, 3})

    assert restarted == [1]


def _fork(workspace_id: int, source_id: int) -> int:
    return get_task_crud().create(
        workspace_id,
        f"fork of {source_id}",
        task_type="fork",
        extra={"fork": {"source_task_id": source_id, "source_message_id": "m"}},
    ).id


def _fork_source(task_id: int) -> object:
    return (get_task_crud().get(task_id).extra or {})["fork"].get("source_task_id")


def test_deleting_a_fork_reparents_its_forks_to_its_source(storage, tmp_path) -> None:
    """A → B → C 删 B：C 改挂到 A；fork 树只少一层，重挂不是对话活动，不刷新更新时间。"""
    workspace = get_workspace_crud().create("forks", str(tmp_path))
    a = get_task_crud().create(workspace.id, "a").id
    b = _fork(workspace.id, a)
    c = _fork(workspace.id, b)
    before = get_task_crud().get(c).updated_at

    get_task_service().delete_task(b)

    assert _fork_source(c) == a
    assert get_task_crud().get(c).updated_at == before


def test_deleting_a_root_keeps_its_forks_as_roots(storage, tmp_path) -> None:
    """被删的不是 fork：它的 fork 不随之删除，去掉来源成为根；委派子 Task 照旧级联删除。"""
    workspace = get_workspace_crud().create("roots", str(tmp_path))
    a = get_task_crud().create(workspace.id, "a").id
    child = get_task_crud().create(
        workspace.id, "child", task_type="delegate_task", parent_task_id=a
    ).id
    b = _fork(workspace.id, a)

    get_task_service().delete_task(a)

    assert _fork_source(b) is None
    with pytest.raises(KeyError):
        get_task_crud().get(child)


def test_deleting_an_idle_task_revokes_its_deferred_work(storage, tmp_path) -> None:
    """后台工作归 Task、比 Run 活得久：空闲 Task 没有执行者，删除提交后也要撤销并终止它。"""
    from app.core.tool.background.deferred_registry import deferred_registry

    workspace = get_workspace_crud().create("idle", str(tmp_path))
    task = get_task_crud().create(workspace.id, "idle").id
    cancelled: list[str] = []
    deferred_registry.register(
        "call-bg",
        task_id=task,
        run_id=None,
        kind="terminal_command",
        label="sleep",
        on_cancel=lambda: cancelled.append("call-bg"),
    )

    get_task_service().delete_task(task)

    assert cancelled == ["call-bg"]
    assert deferred_registry.drop_task(task) == []


@pytest.mark.parametrize("delete_workspace", [False, True])
def test_committed_deletion_closes_task_terminals(storage, tmp_path, monkeypatch, delete_workspace):
    from functools import lru_cache

    from terminal_test_support import active_session_ids, make_service

    from app.service import depends

    workspace = get_workspace_crud().create("terminal", str(tmp_path))
    task = get_task_crud().create(workspace.id, "owner")
    terminal, workers = make_service(monkeypatch)
    terminal.start(
        task_id=task.id, workspace_root=str(tmp_path), run_id=1, created_by="agent"
    )
    monkeypatch.setattr(depends, "get_terminal_session_service", lru_cache()(lambda: terminal))
    try:
        if delete_workspace:
            get_workspace_service().delete_workspace(workspace.id)
        else:
            get_task_service().delete_task(task.id)
        assert workers[0].closed
        assert active_session_ids(terminal, task.id) == []
    finally:
        terminal.shutdown()


def test_failed_task_deletion_does_not_close_or_fence_terminal(storage, tmp_path, monkeypatch):
    from functools import lru_cache

    from terminal_test_support import make_service

    from app.service import depends

    workspace = get_workspace_crud().create("terminal", str(tmp_path))
    task = get_task_crud().create(workspace.id, "owner")
    terminal, workers = make_service(monkeypatch)
    terminal.start(
        task_id=task.id, workspace_root=str(tmp_path), run_id=1, created_by="agent"
    )
    monkeypatch.setattr(depends, "get_terminal_session_service", lru_cache()(lambda: terminal))

    def fail_delete(*args, **kwargs):
        raise RuntimeError("database delete failed")

    monkeypatch.setattr(get_task_service()._task, "delete_by_ids", fail_delete)
    try:
        with pytest.raises(RuntimeError, match="database delete failed"):
            get_task_service().delete_task(task.id)
        assert get_task_crud().get(task.id).id == task.id
        assert not workers[0].closed
        assert (
            terminal.start(
                task_id=task.id, workspace_root=str(tmp_path), run_id=2, created_by="agent"
            ).snapshot().status
            == "running"
        )
    finally:
        terminal.shutdown()


def test_fork_descendants_include_forks_of_forks_only(storage, tmp_path) -> None:
    """A → B → C 与 A → D：A 的 fork 后代是 B、C、D；B 的只有 C；另一个根的 fork 不算。"""
    workspace = get_workspace_crud().create("descendants", str(tmp_path))
    a = get_task_crud().create(workspace.id, "a").id
    b = _fork(workspace.id, a)
    c = _fork(workspace.id, b)
    d = _fork(workspace.id, a)
    other = get_task_crud().create(workspace.id, "other").id
    _fork(workspace.id, other)

    assert get_task_service().collect_fork_descendant_ids(a) == {b, c, d}
    assert get_task_service().collect_fork_descendant_ids(b) == {c}
    assert get_task_service().collect_fork_descendant_ids(c) == frozenset()


def test_deleting_with_forks_removes_fork_trees_and_their_children(storage, tmp_path) -> None:
    """一并删除时，fork 后代连同各自的委派子 Task 一起删；来源不在被删集合里的 fork 不受影响。"""
    workspace = get_workspace_crud().create("with-forks", str(tmp_path))
    a = get_task_crud().create(workspace.id, "a").id
    b = _fork(workspace.id, a)
    c = _fork(workspace.id, b)
    child = get_task_crud().create(
        workspace.id, "child", task_type="delegate_task", parent_task_id=b
    ).id
    other = get_task_crud().create(workspace.id, "other").id
    kept = _fork(workspace.id, other)
    service = get_task_service()

    deleted = service.delete_task(a, service.collect_fork_descendant_ids(a))

    assert deleted == {a, b, c, child}
    for task_id in deleted:
        with pytest.raises(KeyError):
            get_task_crud().get(task_id)
    assert _fork_source(kept) == other


@pytest.mark.asyncio
async def test_delete_task_tree_with_forks_stops_every_fork_tree_first(
    storage, tmp_path, actor_log
) -> None:
    """fork 后代各自的所有权子树也要在删除前停下，删除的就是停下的这批。"""
    workspace = get_workspace_crud().create("stop-forks", str(tmp_path))
    a = get_task_crud().create(workspace.id, "a").id
    b = _fork(workspace.id, a)
    child = get_task_crud().create(
        workspace.id, "child", task_type="delegate_task", parent_task_id=b
    ).id

    deleted = await get_task_service().delete_task_tree(a, with_forks=True)

    assert actor_log.stopped == [{a, b, child}]
    assert deleted == {a, b, child}
