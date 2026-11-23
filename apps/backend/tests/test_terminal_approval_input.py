"""真实输入队列、Actor 和审批等待的接管边界；驱动仅以受控 task 代替。"""

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.assistant_transport.request import SendMessageRequest
from app.assistant_transport.service import assistant_transport_service as transport_module
from app.assistant_transport.service.assistant_transport_service import AssistantTransportService
from app.core.tool.terminal_approval import yielding
from app.core.tool.terminal_approval.registry import terminal_approvals
from app.core.workflow.cancel.run_cancellation_registry import cancellation_registry
from app.core.workflow.message_queue.session_message import RunLaunch, SessionMessage, UserInputBody
from app.core.workflow.message_queue.session_message_queue import SessionMessageQueue
from app.models.message_context.message_metadata.user_message_metadata import UserInputPresentation
from app.models.session.session_command_errors import SessionCommandConflictError
from app.models.session.session_extra import SessionExtra, SessionFork, SessionForkLaunch
from app.models.session.session_kind import SessionType
from app.service import depends
from app.service.session.session_service import SessionService
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces


def user(text="new instruction", *, delivery="immediate"):
    return SessionMessage.of(
        UserInputBody(
            text=text,
            launch=RunLaunch(model_config_id="test/m1"),
            presentation=UserInputPresentation(text=text),
        ),
        delivery=delivery,
    )


def approval(origin, display=None, run_id=100, call="old-call"):
    return terminal_approvals.request(  # noqa: S604 - 登记测试描述，不运行 shell
        origin_session_id=origin,
        display_session_id=display or origin,
        run_id=run_id,
        tool_call_id=call,
        command="must-not-run",
        shell="sh",
        cwd=str(Path.cwd()),
        intent="isolated test",
        origin_title="测试会话",
    )


@pytest.fixture
async def harness(canonical_store, monkeypatch):
    terminal_approvals.close()
    terminal_approvals.set_listener(None)
    queue = SessionMessageQueue()
    session_id = canonical_store.session.id
    run = SimpleNamespace(
        id=100, agent_id="main_agent", terminal_permission_mode="request_approval", status="running"
    )
    sessions = SimpleNamespace(
        get_session=canonical_store.sessions.get,
        require_current_run=lambda _id: run,
        collect_session_tree_ids=lambda _id: [session_id],
    )
    # 显示会话的解析用真实规则（子 Agent 沿所有权找面向用户的会话），只替换它读会话的来源。
    sessions.display_session_id = lambda sid: SessionService.display_session_id(sessions, sid)
    monkeypatch.setattr(depends, "get_session_service", lambda: sessions)
    monkeypatch.setattr(depends, "get_session_message_queue", lambda: queue)
    monkeypatch.setattr(transport_module, "main_db_factory", lambda: canonical_store.factory)
    space = session_runtime_spaces.get_or_create(session_id)
    tasks = []
    driven_sessions = set()

    def driving(space, run_id):
        driven_sessions.add(space.session_id)
        task = asyncio.create_task(asyncio.Event().wait())
        tasks.append(task)
        space.actor._event_loop = asyncio.get_running_loop()
        space.actor._execution._task = task
        space.actor._execution._run = SimpleNamespace(id=run_id)

    driving(space, run.id)
    service = AssistantTransportService.__new__(AssistantTransportService)
    service._sessions, service._queue = sessions, queue
    service._inputs = SimpleNamespace(prepare=lambda *_args, **_kwargs: user().body)
    service._states = SimpleNamespace(refresh_queued_messages=lambda _: None)
    request = SendMessageRequest.model_validate(
        {
            "modelConfigId": "test/m1",
            "message": {"role": "user", "parts": [{"type": "text", "text": "new instruction"}]},
        }
    )
    yield SimpleNamespace(
        service=service,
        space=space,
        queue=queue,
        id=session_id,
        request=request,
        run=run,
        store=canonical_store,
        driving=driving,
    )
    terminal_approvals.close()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    # 受控 task 代替了 SessionExecution._drive，必须承担它对所有驱动（含委派）的信号清理。
    for sid in driven_sessions:
        cancellation_registry.consume(sid)


async def test_plain_send_posts_input_before_releasing_old_approval(harness):
    h = harness
    old = approval(h.id)
    seen = []
    terminal_approvals.set_listener(lambda _id: seen.append(h.space.mailbox.has_user_input()))
    await h.service.send(h.id, h.request)
    assert old.decision == "cancelled" and old.ready.is_set()
    assert old.cancel_cause == "user_message"
    assert seen == [True]
    assert [m.delivery for m in h.queue.pending_nowait(h.id)] == ["immediate"]
    assert h.space.actor._execution._run.id == h.run.id
    assert h.run.terminal_permission_mode == "request_approval"
    assert not cancellation_registry.is_cancelled(h.id)
    assert terminal_approvals.views(h.id) == []
    with pytest.raises(SessionCommandConflictError, match="失效"):
        await h.service.decide_terminal(h.id, old.request_id, "approve")


async def test_plain_send_without_approval_still_queues(harness):
    h = harness
    await h.service.send(h.id, h.request)
    assert [m.delivery for m in h.queue.pending_nowait(h.id)] == ["next_run"]
    assert not h.space.mailbox.has_user_input()


@pytest.mark.parametrize("failure", ["prepare", "commit", "send_now"])
async def test_failed_input_never_revokes_approval(harness, monkeypatch, failure):
    h = harness
    old = approval(h.id)
    if failure == "prepare":

        def fail(*_args, **_kwargs):
            raise ValueError("invalid attachment")

        h.service._inputs.prepare = fail
    elif failure == "commit":

        def fail(*_args, **_kwargs):
            raise RuntimeError("commit failed")

        monkeypatch.setattr(session_runtime_spaces, "enqueue", fail)
    with pytest.raises((ValueError, RuntimeError, SessionCommandConflictError)):
        if failure == "send_now":
            await h.service.send_queued_now(h.id, 999)
        else:
            await h.service.send(h.id, h.request)
    assert old.decision is None and not old.ready.is_set()
    assert not h.space.mailbox.has_user_input()


async def test_send_now_preserves_input_order_and_revokes_only_after_commit(harness):
    h = harness
    queued = h.queue.put(h.id, user("earlier", delivery="next_run"))
    target = h.queue.put(h.id, user("send now", delivery="next_run"))
    old = approval(h.id)
    await h.service.send_queued_now(h.id, target)
    assert old.decision == "cancelled"
    assert h.queue.pending_nowait(h.id)[0].id == queued
    assert [m.body.text for m in h.space.mailbox.ordered()] == ["send now"]


async def test_approval_and_new_input_serialize_on_the_display_gate(harness, monkeypatch):
    h = harness
    old = approval(h.id)
    entered, release = threading.Event(), threading.Event()
    original = h.queue.put

    def hold_commit(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(h.queue, "put", hold_commit)
    send = asyncio.create_task(h.service.send(h.id, h.request))
    assert await asyncio.to_thread(entered.wait, 3)
    approve = asyncio.create_task(h.service.decide_terminal(h.id, old.request_id, "approve"))
    await asyncio.sleep(0)
    assert not approve.done() and old.decision is None
    release.set()
    await send
    with pytest.raises(SessionCommandConflictError):
        await approve
    assert old.decision == "cancelled"


async def test_approval_winning_first_keeps_its_real_result(harness):
    h = harness
    old = approval(h.id)
    await h.service.decide_terminal(h.id, old.request_id, "approve")
    await h.service.send(h.id, h.request)
    assert old.decision == "approve"
    assert [m.delivery for m in h.queue.pending_nowait(h.id)] == ["next_run"]


async def test_late_approval_registration_cannot_block_new_input(harness):
    h = harness
    h.queue.put(h.id, user())
    async with h.space.async_operation():
        await h.space.actor.accept_immediate_input()
    result = await asyncio.to_thread(  # noqa: S604 - 登记测试描述，不运行 shell
        yielding.admit,
        origin_session_id=h.id,
        run_id=100,
        tool_call_id="late",
        command="old",
        shell="sh",
        cwd=str(Path.cwd()),
        intent="late test",
    )
    assert result.cancel_cause == "user_message" and terminal_approvals.views(h.id) == []


async def test_parent_input_stops_only_drives_with_displayed_pending_approval(harness):
    h = harness
    children = [
        h.store.sessions.create(
            h.store.workspace.id,
            f"child-{i}",
            session_type=SessionType.DELEGATED,
            parent_session_id=h.id,
        )
        for i in range(3)
    ]
    spaces = [session_runtime_spaces.get_or_create(s.id) for s in children]
    for index, space in enumerate(spaces):
        h.driving(space, 200 + index)
    first = approval(children[0].id, h.id, 200)
    second = approval(children[1].id, h.id, 201)
    stale = approval(children[2].id, h.id, 999)
    fork = h.store.sessions.create(
        h.store.workspace.id,
        "separate fork",
        session_type=SessionType.AGENT_FORK,
        extra=SessionExtra(
            fork=SessionFork(
                source_session_id=h.id,
                source_title="source",
                tree_parent_session_id=h.id,
                initial_launch=SessionForkLaunch(
                    agent_id="main_agent", model_config_id="test/m1", reasoning_effort=None
                ),
            )
        ),
    )
    own = approval(fork.id, fork.id, 300)
    await h.service.send(h.id, h.request)
    assert all(r.decision == "cancelled" for r in (first, second, stale))
    assert cancellation_registry.is_cancelled(children[0].id)
    assert cancellation_registry.is_cancelled(children[1].id)
    assert not cancellation_registry.is_cancelled(children[2].id)
    assert not cancellation_registry.is_cancelled(h.id)
    assert own.decision is None


async def test_actor_loop_revokes_approval_after_sender_disappears(harness):
    h = harness
    old = approval(h.id)
    h.queue.put(h.id, user())
    pending = h.queue.pending_nowait(h.id)
    await h.space.actor._advance(pending, pending)
    assert old.decision == "cancelled"
    assert h.space.mailbox.has_user_input()
    # loop 旧快照重复投递时不能再次撤销新回合合法审批。
    h.space.mailbox.pop()
    current = approval(h.id, call="new-round")
    await h.space.actor._advance(pending, pending)
    assert current.decision is None
    assert not h.space.mailbox.has_user_input()


async def test_approval_cannot_overtake_committed_input_after_sender_disconnects(harness):
    h = harness
    old = approval(h.id)
    h.queue.put(h.id, user())
    with pytest.raises(SessionCommandConflictError):
        await h.service.decide_terminal(h.id, old.request_id, "approve")
    assert old.decision == "cancelled" and h.space.mailbox.has_user_input()


async def test_late_child_registration_yields_to_the_parent_input(harness):
    h = harness
    child = h.store.sessions.create(
        h.store.workspace.id,
        "late child",
        session_type=SessionType.DELEGATED,
        parent_session_id=h.id,
    )
    space = session_runtime_spaces.get_or_create(child.id)
    h.driving(space, 200)
    h.queue.put(h.id, user())
    async with h.space.async_operation():
        await h.space.actor.accept_immediate_input()
    result = await asyncio.to_thread(  # noqa: S604 - 登记测试描述，不运行 shell
        yielding.admit,
        origin_session_id=child.id,
        run_id=200,
        tool_call_id="late-child",
        command="old",
        shell="sh",
        cwd=str(Path.cwd()),
        intent="late child test",
    )
    assert result.cancel_cause == "user_message"
    assert cancellation_registry.is_cancelled(child.id)
    assert not cancellation_registry.is_cancelled(h.id)
    assert terminal_approvals.views(h.id) == []


async def test_stopping_the_parent_page_leaves_child_approvals_to_the_user(harness):
    """用户停止只打断本会话：自己发起的批准撤销（原因「已停止」），子 Agent 显示在这里的留着。"""
    from app.core.tool.terminal_approval.registry import terminal_approvals as registry

    h = harness
    own = approval(h.id, call="own-call")
    child_request = approval(h.id + 1000, display=h.id, call="child-call")
    registry.cancel_originated(h.id, "stopped")
    assert own.decision == "cancelled" and own.cancel_cause == "stopped"
    assert child_request.decision is None
    assert [view["id"] for view in registry.views(h.id)] == [child_request.request_id]
    registry.cancel(child_request, "call_cancelled")
