"""排队消息的「立即发送」与「删除」命令：在真实 SQLite 外部队列上验证撤回与重新入队。

立即发送撤回原行、以 ``immediate`` 重新入队（新身份，actor 才会当作新到达的消息判定）；删除只撤回。
两者都只作用于仍在队列中的用户输入，已并入或不是用户输入时返回不可重试的 ``MESSAGE_CONSUMED``，
且不改动队列。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.assistant_transport.service import transport_assistant_service as service_module
from app.assistant_transport.service.transport_assistant_service import (
    TransportAssistantService,
)
from app.core.task_actor import SessionConflictError
from app.core.workflow.message_queue.task_message import (
    BackgroundResultBody,
    RunLaunch,
    TaskMessage,
    UserInputBody,
)
from app.core.workflow.message_queue.task_message_queue import TaskMessageQueue
from app.models.conversation_input import UserInputPresentation

_LAUNCH = RunLaunch(model_config_id=5)


def _input(text: str) -> TaskMessage:
    body = UserInputBody(
        input_text=text, launch=_LAUNCH, presentation=UserInputPresentation(display_text=text)
    )
    return TaskMessage.of(body, delivery="next_run")


@pytest.fixture
def harness(canonical_store, monkeypatch):
    queue = TaskMessageQueue()
    monkeypatch.setattr(service_module, "main_session_factory", lambda: canonical_store.factory)
    monkeypatch.setattr(
        service_module,
        "task_runtime_spaces",
        SimpleNamespace(
            enqueue=lambda task_id, message, session=None: queue.put(
                task_id, message, session=session
            )
        ),
    )
    refreshed: list[int] = []
    service = TransportAssistantService.__new__(TransportAssistantService)
    service._queue = queue
    service._states = SimpleNamespace(refresh_queued_messages=refreshed.append)
    return SimpleNamespace(
        queue=queue, service=service, refreshed=refreshed, task_id=canonical_store.task.id
    )


async def test_send_now_requeues_as_immediate_with_a_new_identity(harness) -> None:
    first = harness.queue.put(harness.task_id, _input("first"))
    target = harness.queue.put(harness.task_id, _input("target"))

    await harness.service.send_queued_now(harness.task_id, target)

    rows = harness.queue.pending_nowait(harness.task_id)
    assert [(row.body.input_text, row.delivery) for row in rows] == [
        ("first", "next_run"),
        ("target", "immediate"),
    ]
    assert rows[0].id == first
    assert rows[1].id > target
    assert rows[1].body.launch == _LAUNCH
    assert harness.refreshed == [harness.task_id]


async def test_delete_withdraws_only_the_target(harness) -> None:
    keep = harness.queue.put(harness.task_id, _input("keep"))
    target = harness.queue.put(harness.task_id, _input("drop"))

    await harness.service.delete_queued(harness.task_id, target)

    assert [row.id for row in harness.queue.pending_nowait(harness.task_id)] == [keep]
    assert harness.refreshed == [harness.task_id]


@pytest.mark.parametrize("command", ["send_queued_now", "delete_queued"])
async def test_consumed_message_is_a_non_retryable_conflict(harness, command) -> None:
    with pytest.raises(SessionConflictError) as caught:
        await getattr(harness.service, command)(harness.task_id, 999)

    assert caught.value.code == "MESSAGE_CONSUMED"
    assert caught.value.retryable is False
    assert harness.refreshed == []


@pytest.mark.parametrize("command", ["send_queued_now", "delete_queued"])
async def test_non_user_input_is_rejected_and_kept(harness, command) -> None:
    """撤回在事务里先删后判：不是用户输入时回滚，行保留。"""
    result = harness.queue.put(
        harness.task_id, TaskMessage.of(BackgroundResultBody(tool_name="probe", tool_result="x"))
    )

    with pytest.raises(SessionConflictError):
        await getattr(harness.service, command)(harness.task_id, result)

    assert [row.id for row in harness.queue.pending_nowait(harness.task_id)] == [result]
