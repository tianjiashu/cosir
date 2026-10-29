"""Fork 的事务、历史边界与运行时隔离契约。"""

import asyncio
import threading
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from app.api.schemas import ForkTaskRequest
from app.api.tasks_api import fork_task
from app.assistant_transport.state.message_identity import leading_reply_id, reply_id, turn_of
from app.models.conversation_input import UserInputPresentation
from app.models.errors.task_fork_errors import TaskForkConflictError
from app.models.message_metadata import UserMessageMetadata
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.storage.queries.task_read_queries import TaskReadQueries
from app.task_runtime.service.task_service import TaskService
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.task_runtime.workspace_operation_registry import workspace_operations


def _user_meta(text: str) -> UserMessageMetadata:
    """落库的用户消息都带来源 ``kind``；用户输入还带展示绑定（snapshot 重建与 fork 只从这里取）。"""
    return UserMessageMetadata(
        kind="user_input", presentation=UserInputPresentation(display_text=text)
    )


@pytest.fixture
def fork_store(request, monkeypatch):
    """复用真实 SQLite 夹具，并禁止 Fork 接触任何 Task runtime space。"""
    store = request.getfixturevalue("canonical_store")
    service = TaskService.__new__(TaskService)
    service._task = store.tasks
    service._turn = store.runs
    service._context = store.context_service
    service._session_factory = store.factory
    service._task_reads = TaskReadQueries(store.factory)
    store.service = service
    store.context.create_system_prompt(store.task.id, {"role": "system", "content": "固定系统指令"})

    def runtime_access_forbidden(*args, **kwargs):
        raise AssertionError("fork must not access runtime spaces")

    monkeypatch.setattr(task_runtime_spaces, "get_or_create", runtime_access_forbidden)
    yield store


def append_turn(store, run, text: str) -> str:
    """在 Run 里追加一轮（用户输入 + 回复），返回这一轮回复的 wire 消息 id。

    用户消息身份取 ``run.id * 100 + 序号``：测试内唯一即可，与 Run 号是两个独立序列。
    """
    sequence = store.context.max_sequence(store.task.id) + 1
    message_id = run.id * 100 + sequence
    store.context.append(
        store.task.id,
        run.id,
        {"role": "user", "content": text},
        sequence,
        metadata=_user_meta(text),
        message_id=message_id,
    )
    store.context.append(
        store.task.id, run.id, {"role": "assistant", "content": f"{text}-reply"}, sequence + 1
    )
    return reply_id(message_id)


def append_run(store, status):
    """追加一个只有一轮的 Run；``run.reply`` 是这一轮回复的 wire 消息 id（fork 入口所在）。"""
    run = store.runs.create(store.task.id, status=status)
    run.reply = append_turn(store, run, f"user-{run.id}")
    return run


def fork_call(store, message_id: str):
    """按 API 的换算方式调用 service：wire 消息 id → 所在的一轮。"""
    turn = turn_of(message_id)
    return store.service.fork_task(
        store.task.id,
        user_message_id=turn.user_message_id,
        leading_run_id=turn.leading_run_id,
        source_message_id=message_id,
    )


def fork(store, message_id: str):
    return asyncio.run(fork_call(store, message_id))


def fork_api(store, message_id: str):
    return asyncio.run(
        fork_task(store.task.id, ForkTaskRequest(messageId=message_id), store.service)
    )


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("later_status", ["running", "suspended"])
def test_fork_terminal_history_while_later_run_is_active(fork_store, terminal, later_status):
    store = fork_store
    first = append_run(store, "completed")
    boundary = append_run(store, terminal)
    later = append_run(store, later_status)
    source_rows = store.contexts.get(store.task.id, include_in_context=False)
    target = fork(store, boundary.reply)
    cloned_runs = store.runs.list_by_task(target.id)
    assert [run.status for run in cloned_runs] == ["completed", terminal]
    assert target.task_type == "fork"
    assert target.current_run_id is None
    assert target.tool_schemas == store.task.tool_schemas
    assert target.extra == {
        "fork": {"source_task_id": store.task.id, "source_message_id": boundary.reply}
    }
    assert {run.id for run in cloned_runs}.isdisjoint({first.id, boundary.id, later.id})
    assert all(run.execution_data == ReactExecutionState() for run in cloned_runs)
    target_rows = store.contexts.get(target.id, include_in_context=False)
    assert [row.message for row in target_rows] == [
        row.message for row in source_rows if row.run_id in {None, first.id, boundary.id}
    ]
    assert [row.sequence for row in target_rows] == list(range(5))
    assert {row.run_id for row in target_rows} == {None, *(run.id for run in cloned_runs)}
    assert store.contexts.get(store.task.id, include_in_context=False) == source_rows
    assert store.runs.get(later.id).status == later_status
    assert task_runtime_spaces.existing_spaces() == ()


def test_fork_skips_only_edited_out_rows(fork_store):
    """编辑移出的行不进新 Task；只投影不进上下文（中断输出）与只进上下文不投影（后台结论）的行
    都按原标志复制。"""
    store = fork_store
    run = append_run(store, "completed")
    # 后台结论先并入、再接模型回复：不投影的行在本轮可见行之间，切分点按可见行定位也带得上它。
    unprojected_sequence = store.context.max_sequence(store.task.id) + 1
    store.context.append(
        store.task.id,
        run.id,
        {"role": "user", "content": "background conclusion"},
        unprojected_sequence,
        metadata=_user_meta("background conclusion"),
        project_to_transport=False,
    )
    interrupted_sequence = unprojected_sequence + 1
    store.context.append(
        store.task.id,
        run.id,
        {"role": "assistant", "content": "interrupted"},
        interrupted_sequence,
        include_in_context=False,
    )
    replaced_sequence = interrupted_sequence + 1
    store.context.append(
        store.task.id,
        run.id,
        {"role": "user", "content": "replaced"},
        replaced_sequence,
        metadata=_user_meta("replaced"),
    )
    with store.factory.begin() as session:
        store.context._crud.exclude_from_sequence(store.task.id, replaced_sequence, session)

    target = fork(store, run.reply)

    cloned = store.contexts.get(target.id, include_in_context=False)
    contents = [row.message.get("content") for row in cloned]
    assert "replaced" not in contents
    interrupted = next(row for row in cloned if row.message.get("content") == "interrupted")
    assert (interrupted.include_in_context, interrupted.project_to_transport) == (False, True)
    unprojected = next(
        row for row in cloned if row.message.get("content") == "background conclusion"
    )
    assert (unprojected.include_in_context, unprojected.project_to_transport) == (True, False)


@pytest.mark.parametrize("status", ["running", "suspended"])
@pytest.mark.parametrize("boundary_is_active", [True, False])
def test_reject_active_run_anywhere_in_selected_prefix(fork_store, status, boundary_is_active):
    store = fork_store
    active = append_run(store, status)
    boundary = active if boundary_is_active else append_run(store, "completed")
    with pytest.raises(TaskForkConflictError) as caught:
        fork(store, boundary.reply)
    assert caught.value.code == "RUN_NOT_READY"
    assert len(store.service.list_tasks_for_workspace(store.workspace.id)) == 1


def test_unknown_or_foreign_message_returns_404_without_creating_task(fork_store):
    store = fork_store
    own = append_run(store, "completed")
    other = store.tasks.create(store.workspace.id, "other")
    foreign = store.runs.create(other.id, status="completed")
    for message_id in (leading_reply_id(foreign.id), reply_id(99_999), "99999"):
        with pytest.raises(HTTPException) as caught:
            fork_api(store, message_id)
        assert caught.value.status_code == 404
    assert own.reply
    assert len(store.service.list_tasks_for_workspace(store.workspace.id)) == 2


@pytest.mark.parametrize("message_id", ["assistant-x", "user-1", "0", "assistant-run-"])
def test_malformed_message_id_is_rejected_with_422(fork_store, message_id):
    store = fork_store
    append_run(store, "completed")
    with pytest.raises(HTTPException) as caught:
        fork_api(store, message_id)
    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "FORK_MESSAGE_ID_INVALID"
    assert len(store.service.list_tasks_for_workspace(store.workspace.id)) == 1


def test_fork_request_requires_message_id_and_rejects_run_id():
    """E1：请求只认 ``messageId``；旧的 ``runId`` 形状不再被接受。"""
    from app.assistant_transport.request import TransportRequestError

    assert ForkTaskRequest.model_validate({"messageId": "assistant-3"}).message_id == "assistant-3"
    for payload in ({"runId": 3}, {"messageId": ""}, {}):
        with pytest.raises(TransportRequestError) as caught:
            ForkTaskRequest.model_validate(payload)
        assert caught.value.code == "FORK_MESSAGE_ID_REQUIRED"


def test_fork_inside_a_run_copies_only_turns_up_to_the_message(fork_store):
    """E2：切分点所在 Run 只带到这一轮为止；同一 Run 里更晚的轮次不复制。"""
    store = fork_store
    first = append_run(store, "completed")
    boundary = store.runs.create(store.task.id, status="completed")
    cut = append_turn(store, boundary, "second")
    append_turn(store, boundary, "third")

    target = fork(store, cut)

    contents = [
        row.message.get("content")
        for row in store.contexts.get(target.id, include_in_context=False)
    ]
    assert contents == [
        "固定系统指令",
        f"user-{first.id}",
        f"user-{first.id}-reply",
        "second",
        "second-reply",
    ]
    assert len(store.runs.list_by_task(target.id)) == 2


def test_user_message_and_its_reply_fork_the_same_turn(fork_store):
    """用户消息与它的回复同属一轮：从哪一条分叉，复制范围都相同。"""
    store = fork_store
    run = store.runs.create(store.task.id, status="completed")
    reply = append_turn(store, run, "only")
    append_turn(store, run, "later")
    user = reply.removeprefix("assistant-")

    by_reply = fork(store, reply)
    by_user = fork(store, user)

    def contents(task_id):
        return [
            r.message.get("content") for r in store.contexts.get(task_id, include_in_context=False)
        ]

    assert contents(by_reply.id) == contents(by_user.id) == ["固定系统指令", "only", "only-reply"]


def test_fork_from_leading_reply_of_run_without_user_input(fork_store):
    """没有用户输入的 Run（结果唤醒新开）以 ``assistant-run-{runId}`` 定位，只带开头那段回复。"""
    store = fork_store
    first = append_run(store, "completed")
    woken = store.runs.create(store.task.id, status="completed")
    sequence = store.context.max_sequence(store.task.id) + 1
    store.context.append(
        store.task.id, woken.id, {"role": "assistant", "content": "woken-reply"}, sequence
    )
    append_turn(store, woken, "after")

    target = fork(store, leading_reply_id(woken.id))

    contents = [
        row.message.get("content")
        for row in store.contexts.get(target.id, include_in_context=False)
    ]
    assert contents == ["固定系统指令", f"user-{first.id}", f"user-{first.id}-reply", "woken-reply"]


def test_copy_failure_rolls_back_target_and_runs(fork_store, monkeypatch):
    store = fork_store
    run = append_run(store, "completed")

    def fail_copy(*args, **kwargs):
        raise RuntimeError("copy failed")

    monkeypatch.setattr(store.context_service, "clone_for_fork", fail_copy)
    with pytest.raises(RuntimeError, match="copy failed"):
        fork(store, run.reply)
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
        fork_api(store, run.reply)
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "WORKSPACE_BUSY"
    assert caught.value.detail["retryable"] is True


def test_success_response_does_not_read_database_after_commit(fork_store, monkeypatch):
    store = fork_store
    run = append_run(store, "completed")

    def forbidden(*args):
        raise AssertionError("post-commit read")

    monkeypatch.setattr(store.service, "get_task", forbidden)
    response = fork_api(store, run.reply)
    assert response.task_id != store.task.id
    assert "fork_available" not in response.model_dump()


def test_request_cancellation_does_not_release_workspace_before_worker_finishes(
    fork_store, monkeypatch
):
    store = fork_store
    run = append_run(store, "completed")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_copy = store.context_service.clone_for_fork
    original_worker = store.service._fork_task_in_workspace

    def slow_copy(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original_copy(*args, **kwargs)

    def worker(*args):
        try:
            return original_worker(*args)
        finally:
            finished.set()

    monkeypatch.setattr(store.context_service, "clone_for_fork", slow_copy)
    monkeypatch.setattr(store.service, "_fork_task_in_workspace", worker)

    async def scenario():
        request = asyncio.create_task(fork_call(store, run.reply))
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
