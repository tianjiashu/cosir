"""Real SQLite tests for input-prefix forks, frozen ownership and transaction boundaries."""

import asyncio
import threading
from contextlib import contextmanager
from dataclasses import replace
import pytest
from fastapi import HTTPException
from app.api.schemas import ForkTaskRequest
from app.api.tasks_api import fork_task
from app.assistant_transport.request import TransportRequestError
from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.state.conversation_state_snapshot import validate_snapshot
from app.core.context.tool_call_closure import plan_tool_call_closures, ContextProtocolError
from app.models.conversation_input import MessageSource, UserInputPresentation
from app.models.message_metadata import ToolMessageMetadata, UserMessageMetadata
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
    service._context = store.context_service
    service._session_factory = store.factory
    monkeypatch.setattr(
        "app.core.tool.multi_agent.task_tool_scope.main_agent_tool_schemas", lambda **kw: []
    )
    store.service = service
    store.context.create_system_prompt(store.task.id, {"role": "system", "content": "固定系统指令"})

    def runtime_access_forbidden(*args, **kwargs):
        raise AssertionError("fork must not access runtime spaces")

    monkeypatch.setattr(task_runtime_spaces, "get_or_create", runtime_access_forbidden)
    yield store


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
    )


def input_message(store, run, text, kind="user_input"):
    mid = store.context.max_sequence(store.task.id) + 100
    meta = UserMessageMetadata(
        kind=kind,
        presentation=UserInputPresentation(display_text=text),
        source=MessageSource(task_id=999, name="Parent") if kind == "agent_message" else None,
    )
    append(store, run, {"role": "user", "content": text}, metadata=meta, message_id=mid)
    return str(mid)


def append_run(store, status):
    run = store.runs.create(store.task.id, status=status)
    run.input_id = input_message(store, run, f"input-{run.id}")
    append(store, run, {"role": "assistant", "content": f"reply-{run.id}"})
    return run


def fork_call(store, message_id):
    return store.service.fork_task(store.task.id, before_message_id=int(message_id))


def fork(store, message_id):
    return asyncio.run(fork_call(store, message_id))


def fork_api(store, message_id):
    return asyncio.run(
        fork_task(store.task.id, ForkTaskRequest(beforeMessageId=message_id), store.service)
    )


def snapshot(store, target):
    state = ConversationTaskStateRebuilder.rebuild(
        target,
        store.runs.list_by_task(target.id),
        store.contexts.get(target.id, include_in_context=False),
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
def test_manual_strict_input_prefix_without_executable_runs(fork_store, status):
    s = fork_store
    earlier = append_run(s, "completed")
    current = append_run(s, status)
    child = fork(s, current.input_id)
    assert child.current_run_id is None and s.runs.list_by_task(child.id) == []
    assert [r.message.get("content") for r in s.contexts.get(child.id)] == [
        "固定系统指令",
        f"input-{earlier.id}",
        f"reply-{earlier.id}",
    ]
    state = snapshot(s, child)
    assert state["activity"] is None and len(state["messages"]) == 2
    assert all(m["runId"] is None for m in state["messages"])
    assert state["fork"]["beforeMessageId"] == current.input_id


@pytest.mark.parametrize("kind", ["user_input", "agent_message"])
def test_first_input_can_fork_to_system_only(fork_store, kind):
    s = fork_store
    run = s.runs.create(s.task.id, status="running")
    mid = input_message(s, run, "Do work", kind)
    child = fork(s, mid)
    assert len(s.contexts.get(child.id)) == 1
    assert snapshot(s, child)["messages"] == []


@pytest.mark.parametrize("new_run", [False, True])
def test_agent_excludes_triggering_input_even_in_result_only_run(fork_store, new_run):
    s = fork_store
    earlier = append_run(s, "completed")
    run = append_run(s, "running")
    anchor = run.input_id
    if new_run:
        run = s.runs.create(s.task.id, status="running")
    call(s, run)
    child = agent_fork(s, run)
    assert child.extra.fork.before_message_id == anchor
    assert [r.message.get("content") for r in s.contexts.get(child.id)] == [
        "固定系统指令",
        f"input-{earlier.id}",
        f"reply-{earlier.id}",
    ]
    assert snapshot(s, child)["messages"] == []
    assert all(not r.project_to_transport for r in s.contexts.get(child.id))
    assert s.runs.list_by_task(child.id) == []


def test_manual_fork_of_agent_restores_hidden_history_and_keeps_internal_flags(fork_store):
    s = fork_store
    earlier = append_run(s, "completed")
    append(s, earlier, {"role": "user", "content": "internal fact"},
           metadata=UserMessageMetadata(kind="notice"), project=False)
    run = append_run(s, "running")
    call(s, run)
    child = agent_fork(s, run)
    s.task = child
    local = s.runs.create(child.id, status="running")
    assignment = input_message(s, local, "child assignment", "agent_message")
    manual = fork(s, assignment)
    state = snapshot(s, manual)
    assert [m["parts"][0]["text"] for m in state["messages"]] == [
        f"input-{earlier.id}",
        f"reply-{earlier.id}",
    ]
    assert any(
        r.message.get("content") == "internal fact" and not r.project_to_transport
        for r in s.contexts.get(manual.id)
    )
    assert s.context_service.latest_user_input(manual.id) is None
    assert manual.current_run_id is None


def test_agent_does_not_fall_back_to_inherited_input(fork_store):
    s = fork_store
    append_run(s, "completed")
    current = append_run(s, "running")
    child = fork(s, current.input_id)
    s.task = child
    run = s.runs.create(child.id, status="running")
    call(s, run)
    with pytest.raises(KeyError, match="no local input"):
        agent_fork(s, run)


def test_prefix_repairs_missing_tool_result_without_later_output(fork_store):
    s = fork_store
    run = append_run(s, "running")
    call(s, run, "earlier-call")
    anchor = input_message(s, run, "excluded")
    append(
        s,
        run,
        {"role": "tool", "tool_call_id": "earlier-call", "content": "SECRET LATER RESULT"},
        metadata=ToolMessageMetadata(status="completed"),
    )
    child = fork(s, anchor)
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
    mid = input_message(s, run, "later")
    child = fork(s, mid)
    assert s.context_service.awaiting_decision_tool(child.id, "pending") is None
    assert s.contexts.list_unsettled_placeholders(child.id) == []
    assert snapshot(s, child)["messages"][-1]["parts"][-1]["awaitingUser"] is False
    with s.factory.begin() as session:
        s.contexts.delete_by_task_ids([s.task.id], session)
        s.runs.delete_by_ids([run.id], session)
        s.tasks.delete_by_ids([s.task.id], session)
    assert len(s.contexts.get(child.id)) == 5
    snapshot(s, child)


def test_truncated_run_has_no_later_outcome(fork_store):
    s = fork_store
    run = append_run(s, "failed")
    mid = input_message(s, run, "later failure")
    append(s, run, {"role": "assistant", "content": "later answer"})
    child = fork(s, mid)
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
    mid = input_message(s, run, "cut")
    with pytest.raises(ContextProtocolError):
        fork(s, mid)
    assert len(s.service.list_tasks_for_workspace(s.workspace.id)) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"messageId": "123"},
        {"beforeMessageId": "assistant-123"},
        {"beforeMessageId": "0"},
        {"runId": 1},
    ],
)
def test_api_accepts_only_explicit_input_anchor(payload):
    with pytest.raises(TransportRequestError):
        ForkTaskRequest.model_validate(payload)


def test_unknown_input_returns_404(fork_store):
    with pytest.raises(HTTPException) as caught:
        fork_api(fork_store, "999")
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
        fork_api(store, run.input_id)
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "WORKSPACE_BUSY"
    assert caught.value.detail["retryable"] is True


def test_success_response_does_not_read_database_after_commit(fork_store, monkeypatch):
    store = fork_store
    run = append_run(store, "completed")

    def forbidden(*args):
        raise AssertionError("post-commit read")

    monkeypatch.setattr(store.service, "get_task", forbidden)
    response = fork_api(store, run.input_id)
    assert response.task_id != store.task.id
    assert "fork_available" not in response.model_dump()


def test_request_cancellation_does_not_release_workspace_before_worker_finishes(
    fork_store, monkeypatch
):
    store = fork_store
    run = append_run(store, "completed")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_copy = store.service._copy_prefix
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

    monkeypatch.setattr(store.service, "_copy_prefix", slow_copy)
    monkeypatch.setattr(store.service, "_fork_task_in_workspace", worker)

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


def test_edit_does_not_keep_local_result_because_inherited_call_reuses_id(fork_store):
    s = fork_store
    old = append_run(s, "completed")
    call(s, old, "reused")
    append(
        s,
        old,
        {"role": "tool", "tool_call_id": "reused", "content": "old result"},
        metadata=ToolMessageMetadata(status="completed"),
    )
    next_run = append_run(s, "completed")
    child = fork(s, next_run.input_id)
    s.task = child
    local = s.runs.create(child.id, status="completed")
    input_message(s, local, "edit this")
    anchor = s.context_service.latest_user_input(child.id)
    call(s, local, "reused")
    append(
        s,
        local,
        {"role": "tool", "tool_call_id": "reused", "content": "local result"},
        metadata=ToolMessageMetadata(status="completed"),
    )
    append(
        s,
        local,
        {"role": "user", "content": "local late result"},
        metadata=UserMessageMetadata(kind="background_result", tool_call_id="reused"),
        project=False,
    )
    with s.factory.begin() as session:
        assert s.context_service.exclude_from(child.id, anchor.sequence, session) == {"reused"}
    assert all("local" not in str(r.message) for r in s.contexts.get(child.id))


def test_runtime_controls_ignore_inherited_tool_and_scopes_pair_reused_ids(fork_store):
    from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager

    s = fork_store
    old = append_run(s, "completed")
    call(s, old, "reused")
    append(
        s,
        old,
        {"role": "tool", "tool_call_id": "reused", "content": "old result"},
        metadata=ToolMessageMetadata(status="pending"),
    )
    boundary = append_run(s, "completed")
    child = fork(s, boundary.input_id)
    manager = RuntimeMessageContextManager.__new__(RuntimeMessageContextManager)
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


def test_flattened_history_pairs_repeated_call_ids_by_occurrence(fork_store):
    s = fork_store
    for result in ["FIRST_RESULT", "SECOND_RESULT"]:
        run = append_run(s, "completed")
        call(s, run, "reused")
        append(s, run, {"role": "tool", "tool_call_id": "reused", "content": result},
               metadata=ToolMessageMetadata(status="completed", display_data={"text": result}))
    current = append_run(s, "completed")
    manual = fork(s, current.input_id)
    entries = s.context.entries_in_context(manual.id)
    plans = plan_tool_call_closures(entries)
    assert [entries[p.slots[0].tool_index].message["content"] for p in plans] == [
        "FIRST_RESULT", "SECOND_RESULT"]
    parts = [p for m in snapshot(s, manual)["messages"] for p in m["parts"] if p["type"] == "tool-call"]
    assert len({p["toolCallId"] for p in parts}) == 2
    assert [p["display_data"]["text"] for p in parts] == ["FIRST_RESULT", "SECOND_RESULT"]
    assert [p.slots[0].call_id for p in plans] == ["reused", "reused"]
