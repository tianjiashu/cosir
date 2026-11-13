"""HITL 决定入队在真实主库上的判定：执行中的调用不再等待，并发提交只入队一条。

校验规则见 ``test_tool_decision_entry.py``；这里只测依赖事务的那部分事实。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.workflow.message_queue.session_message import DecisionBody
from app.models.errors.session_command_errors import SessionCommandConflictError
from app.models.message_metadata import ToolMessageMetadata
from app.models.session_context import SessionContextRecord
from app.service import depends
from app.service.conversation import tool_decision_service as module

_CALL_ID = "call-hitl"


@pytest.fixture
def awaiting(tmp_path: Path, isolated_storage):
    workspace = depends.get_workspace_crud().create("ws", str(tmp_path))
    session = depends.get_session_crud().create(workspace.id, "session")
    run = depends.get_run_crud().create(session.id, "suspended")
    crud = depends.get_session_context_crud()
    call = {
        "id": _CALL_ID,
        "type": "function",
        "function": {"name": "hitl_tool", "arguments": "{}"},
    }
    crud.create(
        SessionContextRecord(
            session_id=session.id,
            run_id=run.id,
            message={"role": "assistant", "content": None, "tool_calls": [call]},
            include_in_context=True,
            sequence=1,
            turn=1,
        )
    )
    placeholder = SessionContextRecord(
        session_id=session.id,
        run_id=run.id,
        message={"role": "tool", "tool_call_id": _CALL_ID, "content": "awaiting decision"},
        include_in_context=True,
        sequence=2,
        metadata=ToolMessageMetadata(status="pending"),
        turn=1,
    )
    crud.create(placeholder)
    return SimpleNamespace(session_id=session.id, crud=crud, placeholder=placeholder)


def _queued_decisions(session_id: int) -> list[DecisionBody]:
    return [
        message.body
        for message in depends.get_session_message_queue().pending_nowait(session_id)
        if isinstance(message.body, DecisionBody)
    ]


def test_claimed_call_is_no_longer_awaiting(awaiting) -> None:
    """``prepare_input`` 接收决定后占位是 running：执行期间再提交被拒，原调用不会被重放第二次。"""
    awaiting.crud.replace_message(
        awaiting.session_id,
        awaiting.placeholder.sequence,
        SessionContextRecord(
            session_id=awaiting.session_id,
            run_id=awaiting.placeholder.run_id,
            message=awaiting.placeholder.message,
            include_in_context=True,
            sequence=awaiting.placeholder.sequence,
            metadata=ToolMessageMetadata(status="running"),
            turn=1,
        ),
    )

    with pytest.raises(SessionCommandConflictError) as caught:
        module.ToolDecisionService().submit(awaiting.session_id, _CALL_ID, "approve")

    assert caught.value.code == "DECISION_NOT_AWAITED"
    assert _queued_decisions(awaiting.session_id) == []


def test_concurrent_submissions_enqueue_exactly_one(awaiting, monkeypatch: pytest.MonkeyPatch) -> None:
    """用户交还与运行时代为落定同时提交：判定与入队在一个写事务里，后到的一方看到已提交。"""
    queue = depends.get_session_message_queue()
    original = queue.pending_nowait
    first_checked = threading.Event()

    def slow_first_check(session_id: int, **kwargs):
        pending = original(session_id, **kwargs)
        if not first_checked.is_set():
            # 第一方判定完、入队前停一下，让第二方在这个窗口里判定。
            first_checked.set()
            time.sleep(0.3)
        return pending

    monkeypatch.setattr(queue, "pending_nowait", slow_first_check)
    outcomes: list[str] = []

    def submit(decision: str) -> None:
        try:
            module.ToolDecisionService().submit(awaiting.session_id, _CALL_ID, decision)  # type: ignore[arg-type]
            outcomes.append("enqueued")
        except SessionCommandConflictError as exc:
            outcomes.append(exc.code)

    first = threading.Thread(target=submit, args=("approve",))
    first.start()
    assert first_checked.wait(2)
    second = threading.Thread(target=submit, args=("reject",))
    second.start()
    first.join(5)
    second.join(5)

    assert sorted(outcomes) == ["DECISION_ALREADY_SUBMITTED", "enqueued"]
    [queued] = _queued_decisions(awaiting.session_id)
    assert queued.decision == "approve"
