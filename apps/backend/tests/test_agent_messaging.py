"""普通消息对父、子、同级 Session 均只投递一次；不代替委派回传。"""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.tool.schemas import ToolExecutionContext
from app.core.tool.session_management import addressing, session_send
from app.models.session.session_record import SessionRecord
from app.tools.tool_handler.terminal.command_results import command_results
from tests.session_extra_fixtures import fork_extra


@pytest.fixture(autouse=True)
def _addressing_uses_the_patched_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """寻址规则在 ``addressing``：让它跟着用例给 ``session_send`` 换上的假服务走。"""
    monkeypatch.setattr(
        addressing, "get_session_service", lambda: session_send.get_session_service()
    )


_PARENT = 7
_CHILD = 11


def _session(session_id: int, **fields: Any) -> SessionRecord:
    defaults: dict[str, Any] = {
        "id": session_id,
        "workspace_id": 1,
        "title": f"会话{session_id}",
        "current_run_id": session_id * 10,
        "session_type": "user",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    return SessionRecord(**{**defaults, **fields})


_SESSIONS = {
    _PARENT: _session(_PARENT),
    _CHILD: _session(_CHILD, session_type="delegated", parent_session_id=_PARENT),
}


@pytest.fixture(autouse=True)
def _clean_entries():
    yield
    for session_id in (_PARENT, _CHILD):
        command_results.drop_session(session_id)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    # 收件方当前 Run 的状态，用例可改：决定 session_send 报告的去向。
    state = SimpleNamespace(prepared=[], enqueued=[], delivered=[], run_status={})

    def current(session_id):
        if _SESSIONS[session_id].current_run_id is None:
            return None
        return SimpleNamespace(
            agent_id="main_agent",
            model_config_id="test/m3",
            reasoning_effort="high",
            status=state.run_status.get(session_id, "running"),
            terminal_permission_mode="request_approval",
        )

    monkeypatch.setattr(
        session_send,
        "get_session_service",
        lambda: SimpleNamespace(
            get_session=_SESSIONS.__getitem__,
            require_current_run=current,
            # 这些用例不测权限：都算同一棵所有权树，``session_send`` 不按终端权限拦。
            ownership_root_id=lambda _id: 1,
        ),
    )
    monkeypatch.setattr(
        session_send.session_runtime_spaces,
        "get_or_create",
        lambda _: SimpleNamespace(operation=nullcontext),
    )
    monkeypatch.setattr(session_send, "get_workspace_service", lambda: None)
    monkeypatch.setattr(
        session_send.SessionSendTool,
        "_child_profile_launch",
        lambda self, target, run, ctx: session_send.RunLaunch.from_run(run),
    )

    def prepare(text: str, **kwargs: Any) -> str:
        state.prepared.append({"text": text, **kwargs})
        return f"agent-message:{text}"

    monkeypatch.setattr(session_send, "prepare_agent_message", prepare)

    def enqueue(session_id: int, message: Any) -> None:
        # 登记表交付回复也经这里入队（background_result），按消息类型分开记。
        if isinstance(message, str):
            state.enqueued.append((session_id, message))
        else:
            state.delivered.append((session_id, message.body))

    monkeypatch.setattr(session_send.session_runtime_spaces, "enqueue", enqueue)
    return state


def _context(session_id: int, tool_call_id: str, tmp_path: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        step_id="step-1",
        session_id=session_id,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=session_id * 10,
        event_loop=SimpleNamespace(is_closed=lambda: False),  # type: ignore[arg-type]
        tool_call_id=tool_call_id,
    )


def _send(session_id: int, to: int, text: str, tmp_path: Path, **kwargs: Any):
    return session_send.SessionSendTool().execute(
        to,
        text,
        execution_context=_context(session_id, f"call-{session_id}-{text}", tmp_path),
        **kwargs,
    )


def test_child_tells_its_parent_without_waiting(env, tmp_path) -> None:
    """向上告知：以 Agent 消息投给上级（沿用上级当前 Run 的配置），不登记等待。"""
    observation = _send(_CHILD, _PARENT, "根因找到了", tmp_path)

    assert observation.status == "success"
    assert '"status":"delivered"' in observation.content
    assert observation.background is None
    assert observation.display_data["session_id"] == _PARENT
    assert env.enqueued == [(_PARENT, "agent-message:根因找到了")]
    [prepared] = env.prepared
    assert prepared["source"].session_id == _CHILD
    assert (prepared["launch"].agent_id, prepared["launch"].model_config_id) == ("main_agent", "test/m3")
    assert not command_results.live_background(_CHILD)


def test_result_says_what_the_message_does_at_the_recipient(env, tmp_path) -> None:
    """对照 Claude Code 的 delivered / queued：按投递时收件方的 Run 状态说清去向。"""
    expected = {
        "running": ("running", "it reads your message before its next step"),
        "suspended": ("paused", "your message wakes it"),
        "completed": ("idle", "your message starts a new run"),
        "cancelled": ("idle", "your message starts a new run"),
    }
    for status, (recipient, effect) in expected.items():
        env.run_status[_PARENT] = status
        content = json.loads(_send(_CHILD, _PARENT, f"news-{status}", tmp_path).content)
        assert content == {
            "session_id": _PARENT,
            "status": "delivered",
            "recipient": recipient,
            "effect": effect,
        }


def test_question_and_explicit_reply_are_two_ordinary_messages(env, tmp_path) -> None:
    for sender, target, message in [(_CHILD, _PARENT, "A or B?"), (_PARENT, _CHILD, "A")]:
        sent = _send(sender, target, message, tmp_path)
        assert sent.background is None and '"status":"delivered"' in sent.content
        assert not command_results.live_background(sender)
    assert env.enqueued == [(_PARENT, "agent-message:A or B?"), (_CHILD, "agent-message:A")]
    assert not env.delivered
    # 只有派生方发给自己派生的 Session 才标为 parent：子 Agent 据此把它当任务，其余按信息评估。
    assert [prepared["source"].from_deriver for prepared in env.prepared] == [False, True]


def test_peers_can_send_but_self_and_other_workspace_are_refused(
    env, tmp_path, monkeypatch
) -> None:
    for session in (_session(22), _session(23, workspace_id=2)):
        monkeypatch.setitem(_SESSIONS, session.id, session)
    assert _send(22, _CHILD, "news", tmp_path).status == "success"
    for sender, target in ((_PARENT, _PARENT), (_PARENT, 23)):
        assert _send(sender, target, "hi", tmp_path).status == "error"
    assert env.enqueued == [(_CHILD, "agent-message:news")]


def test_target_without_first_run_inherits_its_queued_launch(env, tmp_path, monkeypatch):
    from app.core.workflow.message_queue.session_message import (
        AgentMessageBody,
        RunLaunch,
        SessionMessage,
    )
    from app.models.message_context.conversation_input import MessageSource

    target = _session(30, current_run_id=None)
    monkeypatch.setitem(_SESSIONS, target.id, target)
    pending = []
    monkeypatch.setattr(
        session_send,
        "get_session_message_queue",
        lambda: SimpleNamespace(pending_nowait=lambda _: pending),
    )
    assert _send(_PARENT, target.id, "hi", tmp_path).status == "error"
    assert not env.enqueued
    pending.append(
        SessionMessage.of(
            AgentMessageBody(
                text="first",
                launch=RunLaunch(model_config_id="test/m7"),
                source=MessageSource(session_id=_PARENT, name="parent"),
            )
        )
    )
    assert _send(_PARENT, target.id, "hi", tmp_path).status == "success"
    assert env.prepared[0]["launch"].model_config_id == "test/m7"


@pytest.mark.parametrize(
    ("fields", "deriver"),
    [
        ({"session_type": "delegated", "parent_session_id": 7}, 7),
        ({"session_type": "agent_fork", "extra": fork_extra(7)}, 7),
        ({"session_type": "user_fork", "extra": fork_extra(7)}, None),
        ({"session_type": "user"}, None),
    ],
)
def test_deriver_is_only_the_agent_that_delegated_or_forked(fields, deriver) -> None:
    assert _session(30, **fields).deriver_id() == deriver


def test_new_manual_fork_uses_frozen_launch_without_source_session(env, tmp_path, monkeypatch):
    target = _session(
        51,
        session_type="user_fork",
        current_run_id=None,
        extra=fork_extra(987654, model_config_id="test/m8"),
    )
    monkeypatch.setitem(_SESSIONS, 51, target)
    monkeypatch.setattr(
        session_send,
        "get_session_message_queue",
        lambda: SimpleNamespace(pending_nowait=lambda _: []),
    )
    assert _send(_PARENT, 51, "continue the branch", tmp_path).status == "success"
    assert env.prepared[-1]["launch"].model_config_id == "test/m8"
    assert env.prepared[-1]["launch"].reasoning_effort == "low"


def test_deleted_fork_model_is_rejected_before_enqueue(
    env, isolated_storage, tmp_path, monkeypatch
):
    """Real model deletion + session_send preparation: a frozen ID never bypasses admission."""
    from app.core.tool.multi_agent.agent_message import prepare_agent_message
    from app.models.session.session_kind import SessionType
    from app.service.depends import get_model_config_service
    from app.storage.crud.session_crud import SessionCrud
    from app.storage.crud.workspace_crud import WorkspaceCrud
    from tests.model_config_fixtures import create_model_config

    model = create_model_config(
        name="fork",
        base_url="http://localhost",
        api_key="test",
        model_name="test",
        context_window_k=8,
        supports_reasoning_effort=True,
    )
    workspace = WorkspaceCrud().create("review", str(tmp_path))
    target = SessionCrud().create(
        workspace.id,
        "branch",
        session_type=SessionType.USER_FORK,
        extra=fork_extra(_PARENT, model_config_id=model.id),
    )
    monkeypatch.setitem(_SESSIONS, target.id, target)
    monkeypatch.setattr(
        session_send,
        "get_session_message_queue",
        lambda: SimpleNamespace(pending_nowait=lambda _: []),
    )
    # Keep the real preparation and its single model-validation boundary.
    monkeypatch.setattr(session_send, "prepare_agent_message", prepare_agent_message)
    get_model_config_service().delete_connection(model.connection.id)
    result = _send(_PARENT, target.id, "continue", tmp_path)
    assert result.status == "error"
    assert "重新选择模型" in result.content
    assert not env.enqueued and not env.delivered
    assert SessionCrud().get(target.id).extra.fork.initial_launch.model_config_id == model.id
