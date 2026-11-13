"""排队消息的「立即发送」与「删除」命令：在真实 SQLite 外部队列上验证撤回与重新入队。

立即发送撤回原行、以 ``immediate`` 重新入队（新身份，actor 才会当作新到达的消息判定）；删除只撤回。
两者都只作用于仍在队列中的用户输入，已并入或不是用户输入时返回不可重试的 ``MESSAGE_CONSUMED``，
且不改动队列。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.assistant_transport.service import assistant_transport_service as service_module
from app.assistant_transport.service.assistant_transport_service import (
    AssistantTransportService,
)
from app.core.workflow.message_queue.session_message import (
    BackgroundResultBody,
    RunLaunch,
    SessionMessage,
    UserInputBody,
)
from app.core.workflow.message_queue.session_message_queue import SessionMessageQueue
from app.models.conversation_input import UserInputPresentation
from app.models.errors.session_command_errors import SessionCommandConflictError

_LAUNCH = RunLaunch(model_config_id="test/m5")


def _input(text: str) -> SessionMessage:
    body = UserInputBody(text=text, launch=_LAUNCH, presentation=UserInputPresentation(text=text))
    return SessionMessage.of(body, delivery="next_run")


@pytest.fixture
def harness(canonical_store, monkeypatch):
    queue = SessionMessageQueue()
    monkeypatch.setattr(service_module, "main_db_factory", lambda: canonical_store.factory)
    monkeypatch.setattr(
        service_module,
        "session_runtime_spaces",
        SimpleNamespace(
            enqueue=lambda session_id, message, db=None: queue.put(
                session_id, message, db=db
            )
        ),
    )
    refreshed: list[int] = []
    service = AssistantTransportService.__new__(AssistantTransportService)
    service._queue = queue
    service._sessions = SimpleNamespace(get_session=lambda _session_id: None)
    service._states = SimpleNamespace(refresh_queued_messages=refreshed.append)
    return SimpleNamespace(
        queue=queue, service=service, refreshed=refreshed, session_id=canonical_store.session.id
    )


async def test_send_now_requeues_as_immediate_with_a_new_identity(harness) -> None:
    first = harness.queue.put(harness.session_id, _input("first"))
    target = harness.queue.put(harness.session_id, _input("target"))

    await harness.service.send_queued_now(harness.session_id, target)

    rows = harness.queue.pending_nowait(harness.session_id)
    assert [(row.body.text, row.delivery) for row in rows] == [
        ("first", "next_run"),
        ("target", "immediate"),
    ]
    assert rows[0].id == first
    assert rows[1].id > target
    assert rows[1].body.launch == _LAUNCH
    assert harness.refreshed == [harness.session_id]


async def test_delete_withdraws_only_the_target(harness) -> None:
    keep = harness.queue.put(harness.session_id, _input("keep"))
    target = harness.queue.put(harness.session_id, _input("drop"))

    await harness.service.delete_queued(harness.session_id, target)

    assert [row.id for row in harness.queue.pending_nowait(harness.session_id)] == [keep]
    assert harness.refreshed == [harness.session_id]


@pytest.mark.parametrize("command", ["send_queued_now", "delete_queued"])
async def test_consumed_message_is_a_non_retryable_conflict(harness, command) -> None:
    with pytest.raises(SessionCommandConflictError) as caught:
        await getattr(harness.service, command)(harness.session_id, 999)

    assert caught.value.code == "MESSAGE_CONSUMED"
    assert caught.value.retryable is False
    assert harness.refreshed == []


@pytest.mark.parametrize("command", ["send_queued_now", "delete_queued"])
async def test_non_user_input_is_rejected_and_kept(harness, command) -> None:
    """撤回在事务里先删后判：不是用户输入时回滚，行保留。"""
    result = harness.queue.put(
        harness.session_id,
        SessionMessage.of(
            BackgroundResultBody(
                ended_at="2026-01-01T00:01:00+00:00", tool_call_id="call-probe",
                tool_name="execute_terminal", tool_result="x",
            )
        ),
    )

    with pytest.raises(SessionCommandConflictError):
        await getattr(harness.service, command)(harness.session_id, result)

    assert [row.id for row in harness.queue.pending_nowait(harness.session_id)] == [result]
