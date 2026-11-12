"""Real SQLite tests for turn-boundary forks, frozen ownership and transaction boundaries."""

import asyncio
import threading
from contextlib import contextmanager

import pytest
from pydantic import ValidationError
from fastapi import HTTPException
from sqlalchemy import update

from app.api.schemas import ForkTaskRequest
from app.api.tasks_api import fork_task
from app.assistant_transport.projection.snapshot_rebuild import (
    rebuild_snapshot,
)
from app.assistant_transport.state.conversation_state_snapshot import validate_snapshot
from app.config.settings import Settings
from app.core.context.tool_call_closure import ContextProtocolError, plan_tool_call_closures
from app.models.conversation_input import MessageSource, UserInputPresentation
from app.models.message_metadata import (
    TURN_INPUT_KINDS,
    BackgroundWork,
    ToolMessageMetadata,
    UserMessageMetadata,
)
from app.service.task.fork_source import ForkSource
from app.service.task.task_service import TaskService
from app.storage.model.task_model import TaskModel
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.task_runtime.workspace_operation_registry import workspace_operations


@pytest.fixture
def fork_store(request, monkeypatch):
    """复用真实 SQLite 夹具，并禁止 Fork 接触任何 Task runtime space。"""
    store = request.getfixturevalue("canonical_store")
    service = TaskService.__new__(TaskService)
    service._task = store.tasks
    service._turn = store.runs
    service._task_context_crud = store.contexts
    service._session_factory = store.factory
    store.service = service
    store.context.create_system_prompt(store.task.id, {"role": "system", "content": "固定系统指令"})

    def runtime_access_forbidden(*args, **kwargs):
        raise AssertionError("fork must not access runtime spaces")

    monkeypatch.setattr(task_runtime_spaces, "get_or_create", runtime_access_forbidden)
    yield store


def _turn_of(store, run, message, metadata, project) -> int:
    """模拟写入方（``RuntimeMessageContextManager._opens_turn``）给夹具行取回合号。

    夹具绕过写入方直接写行，所以在这里按同一规则算：Run 的第一行与用户输入开回合，Agent 消息只在
    本 Run 还没有输入与可见回复时开回合，工具结果属于发起调用的回合，其余行留在当前回合。
    """
    rows = [
        r for r in store.contexts.get(store.task.id, include_in_context=False) if r.sequence > 0
    ]
    current = max((r.turn for r in rows), default=0)
    if message["role"] == "tool":
        # 孤儿结果（用例故意构造的坏数据）没有发起调用，留在当前回合。
        return next(
            (
                r.turn
                for r in reversed(rows)
                if any(
                    c["id"] == message["tool_call_id"] for c in r.message.get("tool_calls") or ()
                )
            ),
            current,
        )
    run_rows = [r for r in rows if r.run_id == run.id]
    if not run_rows:
        return current + 1
    is_input = (
        isinstance(metadata, UserMessageMetadata) and metadata.kind in TURN_INPUT_KINDS and project
    )
    if not is_input:
        return run_rows[-1].turn
    earlier_input = any(
        isinstance(r.metadata, UserMessageMetadata)
        and r.metadata.kind in TURN_INPUT_KINDS
        and r.project_to_transport
        for r in run_rows
    )
    earlier_reply = any(
        r.message["role"] == "assistant"
        and (r.message.get("content") or r.message.get("tool_calls"))
        for r in run_rows
    )
    if metadata.source is None or not (earlier_input or earlier_reply):
        return current + 1
    return run_rows[-1].turn


def append(store, run, message, *, metadata=None, message_id=None, include=True, project=True):
    return store.context.append(
        store.task.id,
        run.id,
        message,
        store.context.max_sequence(store.task.id) + 1,
        include,
        metadata,
        message_id=message_id,
        project_to_transport=project,
        turn=_turn_of(store, run, message, metadata, project),
    )


def input_message(store, run, text, kind="user_input"):
    mid = store.context.max_sequence(store.task.id) + 100
    meta = UserMessageMetadata(
        kind=kind,
        presentation=UserInputPresentation(text=text),
        source=MessageSource(task_id=999, name="Parent") if kind == "agent_message" else None,
    )
    append(store, run, {"role": "user", "content": text}, metadata=meta, message_id=mid)
    return str(mid)


def append_run(store, status):
    run = store.runs.create(store.task.id, status=status)
    run.input_id = input_message(store, run, f"input-{run.id}")
    append(store, run, {"role": "assistant", "content": f"reply-{run.id}"})
    return run


def turn_of_input(store, input_id) -> int:
    """输入所在的回合：它的回复按这个回合号编码。"""
    return next(
        r.turn
        for r in store.contexts.get(store.task.id, include_in_context=False)
        if r.message_id == int(input_id)
    )


def reply_of(store, input_id) -> str:
    """这条输入开启的回合的回复 wire id。"""
    return f"assistant-turn-{turn_of_input(store, input_id)}"


def fork_call(store, input_id):
    return store.service.fork_task(store.task.id, turn=turn_of_input(store, input_id))


def fork(store, input_id):
    """从这条输入的回复往后分叉。"""
    return asyncio.run(fork_call(store, input_id))


def fork_api(store, reply_id):
    return asyncio.run(
        fork_task(store.task.id, ForkTaskRequest(replyMessageId=reply_id), store.service)
    )


def snapshot(store, target):
    state = rebuild_snapshot(
        target,
        store.runs.list_by_task(target.id),
        store.contexts.get(target.id, include_in_context=False), {},
    )
    validate_snapshot(state)
    return state


def call(store, run, call_id="fork-call"):
    append(
        store,
        run,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "fork_task", "arguments": "{}"},
                }
            ],
        },
    )


def agent_fork(store, run, call_id="fork-call"):
    return store.service.fork_for_agent(
        store.task.id, run_id=run.id, tool_call_id=call_id, title="Child", derivation_depth=1
    )


@pytest.mark.parametrize("status", ["running", "suspended", "completed", "failed", "cancelled"])
def test_manual_fork_keeps_the_reply_without_executable_runs(fork_store, status):
    s = fork_store
    earlier = append_run(s, "completed")
    append_run(s, status)
    child = fork(s, earlier.input_id)
    assert child.current_run_id is None and s.runs.list_by_task(child.id) == []
    assert [r.message.get("content") for r in s.contexts.get(child.id)] == [
        "固定系统指令",
        f"input-{earlier.id}",
        f"reply-{earlier.id}",
    ]
    state = snapshot(s, child)
    assert state["activity"] is None and len(state["messages"]) == 2
    assert all(m["inherited"] for m in state["messages"])


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_latest_reply_is_inherited_once_its_run_settled(fork_store, status):
    s = fork_store
    run = append_run(s, status)
    child = fork(s, run.input_id)
    assert [r.message.get("content") for r in s.contexts.get(child.id)][-1] == f"reply-{run.id}"


@pytest.mark.parametrize("status", ["running", "suspended"])
def test_a_reply_still_in_progress_cannot_fork(fork_store, status):
    s = fork_store
    run = append_run(s, status)
    with pytest.raises(HTTPException) as caught:
        fork_api(s, reply_of(s, run.input_id))
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "FORK_REPLY_ACTIVE"


def conversation(state):
    return [p.get("text") or p.get("toolCallId") for m in state["messages"] for p in m["parts"]]


def grouping(state):
    """消息边界：每条消息的角色与它的内容，用来比较两边的回合划分。"""
    return [
        (m["role"], [p.get("text") or p.get("toolCallId") for p in m["parts"]])
        for m in state["messages"]
    ]


def history_with_every_kind_of_turn(s):
    """普通回合、没有输入的唤醒回合、Run 中途并入输入开启的回合、途中收到 Agent 消息的回合。"""
    append_run(s, "completed")
    append_run(s, "completed")
    woken = s.runs.create(s.task.id, status="completed")
    append(
        s,
        woken,
        {"role": "user", "content": "<background_result>"},
        metadata=UserMessageMetadata(kind="background_result"),
        project=False,
    )
    append(s, woken, {"role": "assistant", "content": "woken reply"})
    run = append_run(s, "completed")
    input_message(s, run, "merged input")
    append(s, run, {"role": "assistant", "content": "reply to merged input"})
    letter = append_run(s, "completed")
    input_message(s, letter, "letter on the way", "agent_message")
    append(s, letter, {"role": "assistant", "content": "reply after letter"})


def test_every_reply_forks_to_exactly_the_conversation_up_to_it(fork_store):
    """fork 切点与快照分组都读行上的回合号；这条契约守着两者对每一种回合都给出同一个边界。"""
    s = fork_store
    history_with_every_kind_of_turn(s)
    state = snapshot(s, s.task)
    assert all(
        m["id"].startswith("assistant-turn-") for m in state["messages"] if m["role"] == "assistant"
    )
    forks = []
    for index, message in enumerate(state["messages"]):
        if message["role"] != "assistant":
            continue
        response = fork_api(s, message["id"])
        child = s.service.get_task(response.task_id)
        assert conversation(snapshot(s, child)) == conversation(
            {"messages": state["messages"][: index + 1]}
        ), message["id"]
        forks.append(child)
    # 回合号随行复制：分支的回合划分与来源一致，同一条回复在两边的 id 相同（都按回合编码），
    # 唤醒开出的回复仍是单独一条，并且同样能再分叉。
    s.task = forks[-1]
    branch = snapshot(s, s.task)
    assert grouping(branch) == grouping(state)
    assert [m["id"] for m in branch["messages"] if m["role"] == "assistant"] == [
        m["id"] for m in state["messages"] if m["role"] == "assistant"
    ]
    assert all(m["inherited"] for m in branch["messages"])
    for index, message in enumerate(branch["messages"]):
        if message["role"] == "assistant":
            refork = s.service.get_task(fork_api(s, message["id"]).task_id)
            assert conversation(snapshot(s, refork)) == conversation(
                {"messages": branch["messages"][: index + 1]}
            ), message["id"]


def test_fork_starts_from_the_launch_of_the_replys_run(fork_store):
    s = fork_store
    first = s.runs.create(s.task.id, status="completed", agent_id="reply-agent")
    first.input_id = input_message(s, first, "first")
    append_run(s, "completed")
    child = fork(s, first.input_id)
    assert child.extra.fork.initial_launch.agent_id == "reply-agent"


def test_agent_forks_from_the_previous_reply_excluding_the_current_turn(fork_store):
    s = fork_store
    earlier = append_run(s, "completed")
    run = append_run(s, "running")
    call(s, run)
    child = agent_fork(s, run)
    assert [r.message.get("content") for r in s.contexts.get(child.id)] == [
        "固定系统指令",
        f"input-{earlier.id}",
        f"reply-{earlier.id}",
    ]
    assert snapshot(s, child)["messages"] == []
    assert all(not r.project_to_transport for r in s.contexts.get(child.id))
    assert s.runs.list_by_task(child.id) == []


def test_agent_in_a_run_without_input_keeps_the_previous_reply(fork_store):
    """没有输入的回合从 Run 开头算起：上一条回复已经结束，不能跟着更早的输入一起丢掉。"""
    s = fork_store
    previous = append_run(s, "completed")
    run = s.runs.create(s.task.id, status="running")
    append(
        s,
        run,
        {"role": "user", "content": "<background_result>"},
        metadata=UserMessageMetadata(kind="background_result"),
        project=False,
    )
    append(s, run, {"role": "assistant", "content": "current turn"})
    call(s, run)
    child = agent_fork(s, run)
    assert [r.message.get("content") for r in s.contexts.get(child.id)][-2:] == [
        f"input-{previous.id}",
        f"reply-{previous.id}",
    ]


def test_agent_fork_excludes_the_whole_turn_a_letter_arrived_in(fork_store):
    """途中来信不开回合：切点仍在用户这一轮的消息之前，和手动分叉同一条规则。"""
    s = fork_store
    earlier = append_run(s, "completed")
    run = append_run(s, "running")
    input_message(s, run, "letter on the way", "agent_message")
    call(s, run)
    child = agent_fork(s, run)
    assert [r.message.get("content") for r in s.contexts.get(child.id)] == [
        "固定系统指令",
        f"input-{earlier.id}",
        f"reply-{earlier.id}",
    ]


@pytest.mark.parametrize("depth", [1, 2])
def test_agent_fork_keeps_the_source_tools_at_any_depth(fork_store, monkeypatch, depth):
    """工具列表是请求前缀的一部分：到深度上限也不改，派生限制交给运行期准入。"""
    s = fork_store
    tools = [{"name": "read_file"}, {"name": "fork_task"}, {"name": "delegate_task"}]
    with s.factory() as session:
        session.execute(
            update(TaskModel).where(TaskModel.id == s.task.id).values(tool_schemas=tools)
        )
        session.commit()
    monkeypatch.setattr(Settings, "AGENT_MAX_DERIVATION_DEPTH", 2)
    run = append_run(s, "running")
    call(s, run)
    child = s.service.fork_for_agent(
        s.task.id, run_id=run.id, tool_call_id="fork-call", title="Child", derivation_depth=depth
    )
    assert child.tool_schemas == tools


def test_manual_fork_cuts_after_the_whole_reply_a_letter_arrived_in(fork_store):
    s = fork_store
    run = append_run(s, "completed")
    input_message(s, run, "letter on the way", "agent_message")
    append(s, run, {"role": "assistant", "content": "reply after letter"})
    later = append_run(s, "completed")
    child = fork(s, run.input_id)
    contents = [r.message.get("content") for r in s.contexts.get(child.id)]
    assert contents[-1] == "reply after letter" and f"input-{later.id}" not in contents


def test_agent_on_the_first_turn_forks_to_system_only(fork_store):
    s = fork_store
    run = append_run(s, "running")
    call(s, run)
    child = agent_fork(s, run)
    assert [r.message.get("content") for r in s.contexts.get(child.id)] == ["固定系统指令"]


def test_agent_in_a_branch_inherits_the_branch_history(fork_store):
    s = fork_store
    earlier = append_run(s, "completed")
    s.task = fork(s, earlier.input_id)
    run = s.runs.create(s.task.id, status="running")
    call(s, run)
    child = agent_fork(s, run)
    assert f"reply-{earlier.id}" in [r.message.get("content") for r in s.contexts.get(child.id)]


def test_manual_fork_of_agent_restores_hidden_history_and_keeps_internal_flags(fork_store):
    s = fork_store
    earlier = append_run(s, "completed")
    append(
        s,
        earlier,
        {"role": "user", "content": "internal fact"},
        metadata=UserMessageMetadata(kind="notice"),
        project=False,
    )
    run = append_run(s, "running")
    call(s, run)
    child = agent_fork(s, run)
    s.task = child
    local = s.runs.create(child.id, status="completed")
    assignment = input_message(s, local, "child assignment", "agent_message")
    append(s, local, {"role": "assistant", "content": "child reply"})
    manual = fork(s, assignment)
    state = snapshot(s, manual)
    assert [m["parts"][0]["text"] for m in state["messages"]] == [
        f"input-{earlier.id}",
        f"reply-{earlier.id}",
        "child assignment",
        "child reply",
    ]
    assert any(
        r.message.get("content") == "internal fact" and not r.project_to_transport
        for r in s.contexts.get(manual.id)
    )
    assert s.context_service.latest_user_input(manual.id) is None
    assert manual.current_run_id is None


def test_prefix_repairs_missing_tool_result_without_later_output(fork_store):
    s = fork_store
    run = append_run(s, "running")
    call(s, run, "earlier-call")
    input_message(s, run, "excluded")
    append(
        s,
        run,
        {"role": "tool", "tool_call_id": "earlier-call", "content": "SECRET LATER RESULT"},
        metadata=ToolMessageMetadata(status="completed"),
    )
    child = fork(s, run.input_id)
    entries = s.context.entries_in_context(child.id)
    assert all(not plan.missing_slots for plan in plan_tool_call_closures(entries))
    body = str([entry.message for entry in entries])
    assert "SECRET LATER RESULT" not in body
    assert "not inherited" in body
    assert snapshot(s, child)["messages"][-1]["outcome"] is None


def test_pending_history_is_inert_and_source_deletion_does_not_remove_it(fork_store):
    s = fork_store
    run = append_run(s, "running")
    call(s, run, "pending")
    append(
        s,
        run,
        {"role": "tool", "tool_call_id": "pending", "content": "Awaiting user"},
        metadata=ToolMessageMetadata(status="pending"),
    )
    input_message(s, run, "later")
    child = fork(s, run.input_id)
    assert s.context_service.awaiting_decision_tool(child.id, "pending") is None
    assert s.contexts.list_unsettled_placeholders(child.id) == []
    part = snapshot(s, child)["messages"][-1]["parts"][-1]
    assert part["status"] == "cancelled" and part["awaitingUser"] is False
    assert "not inherited" in str(s.contexts.get(child.id)[-1].message)
    with s.factory.begin() as session:
        s.contexts.delete_by_task_ids([s.task.id], session)
        s.runs.delete_by_ids([run.id], session)
        s.tasks.delete_by_ids([s.task.id], session)
    assert len(s.contexts.get(child.id)) == 5
    snapshot(s, child)


def background_placeholder(s, run, call_id, status):
    call(s, run, call_id)
    work = BackgroundWork(
        kind="terminal_command", run_id=run.id, label="work", started_at="2026-01-01T00:00:00Z"
    )
    append(
        s,
        run,
        {"role": "tool", "tool_call_id": call_id, "content": '{"status":"submitted"}'},
        metadata=ToolMessageMetadata(status=status, background=work),
    )


def test_call_answered_after_the_cut_is_not_inherited_as_submitted(fork_store):
    """结论写在切点后面时，占位正文只剩「已提交」，模型会以为工作还在进行。"""
    s = fork_store
    run = append_run(s, "running")
    background_placeholder(s, run, "bg", "completed")
    input_message(s, run, "next turn")
    append(
        s,
        run,
        {"role": "user", "content": "<background_result>real conclusion"},
        metadata=UserMessageMetadata(kind="background_result", tool_call_id="bg"),
        project=False,
    )
    child = fork(s, run.input_id)
    body = str([r.message for r in s.contexts.get(child.id)])
    assert "submitted" not in body and "real conclusion" not in body
    assert "not inherited" in body
    part = snapshot(s, child)["messages"][-1]["parts"][-1]
    assert part["status"] == "cancelled" and "background" not in part


def test_call_answered_before_the_cut_keeps_its_placeholder_and_conclusion(fork_store):
    s = fork_store
    run = append_run(s, "running")
    background_placeholder(s, run, "bg", "completed")
    append(
        s,
        run,
        {"role": "user", "content": "<background_result>real conclusion"},
        metadata=UserMessageMetadata(kind="background_result", tool_call_id="bg"),
        project=False,
    )
    input_message(s, run, "next turn")
    child = fork(s, run.input_id)
    body = str([r.message for r in s.contexts.get(child.id)])
    assert "submitted" in body and "real conclusion" in body


def test_truncated_run_has_no_later_outcome(fork_store):
    s = fork_store
    run = append_run(s, "failed")
    input_message(s, run, "later failure")
    append(s, run, {"role": "assistant", "content": "later answer"})
    child = fork(s, run.input_id)
    assert all(m["outcome"] is None for m in snapshot(s, child)["messages"])


def test_orphan_protocol_aborts_whole_copy(fork_store):
    s = fork_store
    run = append_run(s, "completed")
    append(
        s,
        run,
        {"role": "tool", "tool_call_id": "orphan", "content": "bad"},
        metadata=ToolMessageMetadata(status="completed"),
    )
    input_message(s, run, "cut")
    with pytest.raises(ContextProtocolError):
        fork(s, run.input_id)
    assert len(s.service.list_tasks_for_workspace(s.workspace.id)) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"beforeMessageId": "123"},
        {"replyMessageId": "123"},
        {"replyMessageId": "assistant-0"},
        {"replyMessageId": "assistant-12"},
        {"replyMessageId": "assistant-run-3"},
        {"replyMessageId": "assistant-turn-0"},
        {"replyMessageId": "assistant-run-x"},
        {"replyMessageId": "assistant-context"},
        {"runId": 1},
    ],
)
def test_api_accepts_only_a_reply_id(payload):
    with pytest.raises(ValidationError):
        ForkTaskRequest.model_validate(payload)


def test_api_decodes_the_reply_into_its_turn():
    assert ForkTaskRequest(replyMessageId="assistant-turn-5").turn == 5


def test_unknown_reply_returns_404(fork_store):
    with pytest.raises(HTTPException) as caught:
        fork_api(fork_store, "assistant-turn-999")
    assert caught.value.status_code == 404


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
        fork_api(store, reply_of(store, run.input_id))
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "WORKSPACE_BUSY"
    assert caught.value.detail["retryable"] is True


def test_success_response_does_not_read_database_after_commit(fork_store, monkeypatch):
    store = fork_store
    run = append_run(store, "completed")

    def forbidden(*args):
        raise AssertionError("post-commit read")

    monkeypatch.setattr(store.service, "get_task", forbidden)
    response = fork_api(store, reply_of(store, run.input_id))
    assert response.task_id != store.task.id
    assert "fork_available" not in response.model_dump()


def test_request_cancellation_does_not_release_workspace_before_worker_finishes(
    fork_store, monkeypatch
):
    store = fork_store
    run = append_run(store, "completed")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_copy = ForkSource.copy_before
    original_worker = store.service._fork_at_turn

    def slow_copy(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original_copy(*args, **kwargs)

    def worker(*args):
        try:
            return original_worker(*args)
        finally:
            finished.set()

    monkeypatch.setattr(ForkSource, "copy_before", slow_copy)
    monkeypatch.setattr(store.service, "_fork_at_turn", worker)

    async def scenario():
        request = asyncio.create_task(fork_call(store, run.input_id))
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


def test_runtime_controls_ignore_inherited_tool_and_scopes_pair_reused_ids(fork_store):
    from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager

    s = fork_store
    old = append_run(s, "completed")
    call(s, old, "reused")
    append(
        s,
        old,
        {"role": "tool", "tool_call_id": "reused", "content": "old result"},
        metadata=ToolMessageMetadata(status="completed"),
    )
    child = fork(s, old.input_id)
    manager = RuntimeMessageContextManager.__new__(RuntimeMessageContextManager)
    manager._used_call_ids = set()
    manager._response_call_ids = {}
    manager._entries = s.context.entries_in_context(child.id)
    assert manager.find_tool_call("reused") is None
    assert manager.tool_result_metadata("reused") is None
    with pytest.raises(KeyError):
        manager.update_tool_result_metadata("reused", ToolMessageMetadata(status="completed"))
    s.task = child
    local = s.runs.create(child.id, status="completed")
    input_message(s, local, "local task")
    call(s, local, "reused")
    append(
        s,
        local,
        {"role": "tool", "tool_call_id": "reused", "content": "local result"},
        metadata=ToolMessageMetadata(status="completed"),
    )
    entries = s.context.entries_in_context(child.id)
    plans = plan_tool_call_closures(entries)
    assert len(plans) == 2
    assert all(not p.missing_slots for p in plans)
    assert entries[plans[0].slots[0].tool_index].message["content"] == "old result"
    assert entries[plans[1].slots[0].tool_index].message["content"] == "local result"


def test_interrupted_output_survives_hidden_fork_and_manual_refork(fork_store):
    s = fork_store
    earlier = append_run(s, "cancelled")
    append(s, earlier, {"role": "assistant", "content": "PARTIAL_OUTPUT"}, include=False)
    current = append_run(s, "running")
    call(s, current)
    child = agent_fork(s, current)
    copied = s.contexts.get(child.id, include_in_context=False)
    partial = next(r for r in copied if r.message.get("content") == "PARTIAL_OUTPUT")
    assert partial.retained and not partial.include_in_context and not partial.project_to_transport
    s.task = child
    local = s.runs.create(child.id, status="completed")
    anchor = input_message(s, local, "new assignment", "agent_message")
    manual = fork(s, anchor)
    assert "PARTIAL_OUTPUT" in str(snapshot(s, manual)["messages"])
    assert "PARTIAL_OUTPUT" not in str([r.message for r in s.contexts.get(manual.id)])
