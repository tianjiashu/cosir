"""Task 命令的纯投递契约（真实 SQLite）：投递即 202、生产者侧输入错误同步返回、actor 拒绝的命令
写进 snapshot 顶层 ``error`` 并在下一次成功的用户命令后清空。

设计见 ``docs/plan/task-command-fire-and-forget-design.md``（验收 B1 / B3）。
"""

import asyncio

import pytest
from fastapi import HTTPException

from app.assistant_transport.assistant_api import (
    cancel_task,
    rerun_message,
    resume_task,
    send_message,
    submit_decision,
)
from app.assistant_transport.request import (
    RerunMessageRequest,
    SendMessageRequest,
    TeamDecisionRequest,
)
from app.models import ConversationRunCommand
from app.models.errors.input_errors import UserInputRejectedError
from app.service.conversation_run.task_signals import Rerun
from app.service.conversation_run.user_input_service import UserInputService
from app.service.depends import get_task_service
from tests import test_task_input_queue as queue_tests
from tests.test_task_input_queue import _command_error, _consume, _processed, _session, _starts

# 复用队列用例的真实 SQLite fixture 与会话清理。
store = queue_tests.store
_close_sessions = queue_tests._close_sessions

_MISSING_TASK = 999_999


def _send_request(store, text: str) -> SendMessageRequest:
    return SendMessageRequest.model_validate(
        {
            "message": {"role": "user", "parts": [{"type": "text", "text": text}]},
            "modelConfigId": store.config_id,
        }
    )


def _rerun_request(store, text: str) -> RerunMessageRequest:
    return RerunMessageRequest.model_validate(
        {
            "message": {"role": "user", "parts": [{"type": "text", "text": text}]},
            "modelConfigId": store.config_id,
        }
    )


async def test_send_is_accepted_with_empty_body_and_no_snapshot(store, monkeypatch):
    """B1：发送投递即 202，响应体是 ``{}``；处理结果只经 snapshot 交给前端。"""
    session, _executor = _session(monkeypatch)

    response = await send_message(store.task.id, _send_request(store, "second"), actors=session)

    assert response.status_code == 202
    assert response.body == b"{}"


async def test_resume_and_cancel_of_existing_task_are_accepted(store, monkeypatch):
    """B1：恢复与取消不解析输入，确认 Task 存在后投递即 202；是否生效只看 snapshot。"""
    session, _executor = _session(monkeypatch)
    tasks = get_task_service()

    resumed = await resume_task(store.task.id, actors=session, task_service=tasks)
    cancelled = await cancel_task(store.task.id, actors=session, task_service=tasks)

    assert (resumed.status_code, resumed.body) == (202, b"{}")
    assert (cancelled.status_code, cancelled.body) == (202, b"{}")


async def test_commands_on_missing_task_are_404(store, monkeypatch):
    """B1：Task 不存在是生产者自己的输入问题，同步 404，不进 actor。"""
    session, _executor = _session(monkeypatch)
    tasks = get_task_service()
    calls = [
        send_message(_MISSING_TASK, _send_request(store, "x"), actors=session),
        rerun_message(_MISSING_TASK, 1, _rerun_request(store, "x"), actors=session),
        resume_task(_MISSING_TASK, actors=session, task_service=tasks),
        cancel_task(_MISSING_TASK, actors=session, task_service=tasks),
        submit_decision(
            _MISSING_TASK,
            TeamDecisionRequest.model_validate(
                {"teamRunId": 999_999, "decision": "reject", "data": {}}
            ),
        ),
    ]
    for call in calls:
        with pytest.raises(HTTPException) as caught:
            await call
        assert caught.value.status_code == 404
    assert _MISSING_TASK not in session._actors


async def test_rejected_input_is_400_and_not_delivered(store, monkeypatch):
    """B1：输入在投递前被拒（附件不可用、模型不支持等）时同步 400，actor 不会收到信号。

    wire 形状错误（缺字段、空 parts）由 FastAPI 请求校验在进入路由前拒绝（422），不在本层。
    """
    session, executor = _session(monkeypatch)

    def reject(*_args, **_kwargs):
        raise UserInputRejectedError("ATTACHMENT_UNAVAILABLE", "附件不可用", detail="att-1")

    monkeypatch.setattr(UserInputService, "prepare", reject)

    with pytest.raises(HTTPException) as caught:
        await send_message(store.task.id, _send_request(store, "x"), actors=session)

    assert caught.value.status_code == 400
    assert store.task.id not in session._actors
    executor.start.assert_not_called()


async def test_unresumable_resume_projects_error_and_next_send_clears_it(store, monkeypatch):
    """B3：Run 已完成时恢复被 actor 拒绝，原因写进顶层 ``error``；下一次成功的发送清空它。"""
    _consume(store, store.run.id)
    store.crud.update_status_if_in(store.run.id, "completed", ("running",))
    session, executor = _session(monkeypatch)

    session.resume(store.task.id)
    await _processed(session, store.task.id)
    error = _command_error(store.task.id)
    assert error is not None and error["code"] == "RUN_NOT_RESUMABLE"

    await session.submit(
        store.task.id,
        ConversationRunCommand(display_text="next"),
        model_config_id=store.config_id,
        reasoning_effort=None,
    )
    await _processed(session, store.task.id)
    assert _command_error(store.task.id) is None
    assert len(_starts(executor)) == 1


async def test_stale_rerun_target_projects_edit_target_stale(store, monkeypatch):
    """B3：停止期间目标已不是最新一条用户输入时，第二阶段拒绝（``EDIT_TARGET_STALE``），不移出
    任何 context；之后成功的恢复会清空这条错误。"""
    [first] = _consume(store, store.run.id)
    store.crud.update_status_if_in(store.run.id, "cancelled", ("running",))
    session, _executor = _session(monkeypatch)
    body = UserInputService().prepare(
        store.task.id, ConversationRunCommand(display_text="edited"), store.config_id
    )

    actor = session._actor(store.task.id)
    changed = actor.state_changed
    actor.send(Rerun(first.message_id + 1000, body))
    await asyncio.wait_for(changed.wait(), 2)

    error = _command_error(store.task.id)
    assert error is not None and error["code"] == "EDIT_TARGET_STALE"
    assert [e.message_id for e in store.context.entries_in_context(store.task.id)] == [
        first.message_id
    ]

    session.resume(store.task.id)
    await _processed(session, store.task.id)
    assert _command_error(store.task.id) is None


async def test_system_message_new_run_keeps_command_error(store, monkeypatch):
    """缺陷修复：已完成 Task 收到后台结果新开 Run 会重建 snapshot，但命令错误只该由用户命令清空，
    重建不能把它丢掉。"""
    from app.models.task_message import BackgroundResultBody

    _consume(store, store.run.id)
    store.crud.update_status_if_in(store.run.id, "completed", ("running",))
    session, executor = _session(monkeypatch)
    session.resume(store.task.id)
    await _processed(session, store.task.id)
    rejected = _command_error(store.task.id)
    assert rejected is not None and rejected["code"] == "RUN_NOT_RESUMABLE"

    session.post(
        store.task.id,
        BackgroundResultBody(job="agent_team", job_id=5, status="completed", content="{}"),
    )
    await _processed(session, store.task.id)

    assert len(_starts(executor)) == 1
    assert _command_error(store.task.id) == rejected
