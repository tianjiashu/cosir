"""Run 落定后作废等用户回应的决定：真实主库上的结算（接入点见 ``test_session_actor_loop.py`` 与
``test_recover_after_restart.py``）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.workflow.decision_expiry import expire_decisions_after_run
from app.models.errors.session_command_errors import SessionCommandConflictError
from app.models.message_metadata import ToolMessageMetadata
from app.models.session_context import SessionContextRecord
from app.service import depends
from app.service.conversation import tool_decision_service


@pytest.fixture
def asked(tmp_path: Path, isolated_storage, monkeypatch: pytest.MonkeyPatch):
    """一个 Session 里一个等用户回应的提问占位。"""
    definitions = {"ask_tool": SimpleNamespace(user_decision=True, decision_model=None)}
    registry = SimpleNamespace(get_tool_definition=definitions.get)
    monkeypatch.setattr(tool_decision_service, "get_tool_registry", lambda: registry)
    workspace = depends.get_workspace_crud().create("ws", str(tmp_path))
    session = depends.get_session_crud().create(workspace.id, "session")
    run = depends.get_run_crud().create(session.id, "cancelled")
    crud = depends.get_session_context_crud()
    calls = [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}
        for call_id, name in (("call-ask", "ask_tool"),)
    ]
    crud.create(
        SessionContextRecord(
            session_id=session.id, run_id=run.id, include_in_context=True, sequence=1, turn=1,
            message={"role": "assistant", "content": None, "tool_calls": calls},
        )
    )
    for sequence, call in enumerate(calls, start=2):
        crud.create(
            SessionContextRecord(
                session_id=session.id, run_id=run.id, include_in_context=True, sequence=sequence,
                turn=1, metadata=ToolMessageMetadata(status="pending", display_data={"kind": "probe"}),
                message={"role": "tool", "tool_call_id": call["id"], "content": "awaiting decision"},
            )
        )
    return session.id


def _placeholder(session_id: int, call_id: str) -> ToolMessageMetadata:
    [row] = [
        record
        for record in depends.get_session_context_crud().get(session_id)
        if record.message.get("tool_call_id") == call_id
    ]
    assert isinstance(row.metadata, ToolMessageMetadata)
    return row.metadata


def test_awaiting_decisions_expire_when_the_run_settles(asked: int) -> None:
    """提问随 Run 落定作废：只改卡片状态，展示数据保留。"""
    assert expire_decisions_after_run(asked, "已停止") == ["call-ask"]

    question = _placeholder(asked, "call-ask")
    assert (question.status, question.error) == ("cancelled", "已停止")
    assert question.display_data == {"kind": "probe"}
    # 已结算的不再动：重复调用幂等。
    assert expire_decisions_after_run(asked, "未作答") == []
    assert _placeholder(asked, "call-ask").error == "已停止"


def test_expired_question_can_no_longer_be_answered(asked: int) -> None:
    """作废后决定入口不再认它在等待：回答不会在 Run 结束之后开出新 Run。"""
    expire_decisions_after_run(asked, "已停止")

    with pytest.raises(SessionCommandConflictError) as rejected:
        tool_decision_service.ToolDecisionService().submit(asked, "call-ask", "approve")

    assert rejected.value.code == "DECISION_NOT_AWAITED"
    assert depends.get_session_context_service().awaiting_decision_calls(asked) == {}
