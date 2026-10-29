"""HITL 决定入队在真实主库上的判定：执行中的调用不再等待，并发提交只入队一条。

校验与载荷规则见 ``test_tool_decision_entry.py``；这里只测依赖事务的那部分事实。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.task_actor import SessionConflictError
from app.core.workflow.message_queue.task_message import DecisionBody
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.message_metadata import ToolMessageMetadata
from app.service import depends
from app.service.conversation_run import tool_decision_service as module

_CALL_ID = "call-hitl"


@pytest.fixture
def awaiting(tmp_path: Path, isolated_storage, monkeypatch: pytest.MonkeyPatch):
    workspace = depends.get_workspace_crud().create("ws", str(tmp_path))
    task = depends.get_task_crud().create(workspace.id, "task")
    run = depends.get_conversation_run_crud().create(task.id, "suspended")
    crud = depends.get_conversation_task_context_crud()
    call = {
        "id": _CALL_ID,
        "type": "function",
        "function": {"name": "hitl_tool", "arguments": "{}"},
    }
    crud.create(
        ConversationTaskContextRecord(
            task_id=task.id,
            run_id=run.id,
            message={"role": "assistant", "content": None, "tool_calls": [call]},
            include_in_context=True,
            sequence=1,
        )
    )
    placeholder = ConversationTaskContextRecord(
        task_id=task.id,
        run_id=run.id,
        message={"role": "tool", "tool_call_id": _CALL_ID, "content": "awaiting decision"},
        include_in_context=True,
        sequence=2,
        metadata=ToolMessageMetadata(status="pending"),
    )
    crud.create(placeholder)
    monkeypatch.setattr(
        module,
        "get_tool_registry",
        lambda: SimpleNamespace(get_tool_definition=lambda _name: SimpleNamespace(decision_model=None)),
    )
    return SimpleNamespace(task_id=task.id, crud=crud, placeholder=placeholder)


def _queued_decisions(task_id: int) -> list[DecisionBody]:
    return [
        message.body
        for message in depends.get_task_message_queue().pending_nowait(task_id)
        if isinstance(message.body, DecisionBody)
    ]


def test_claimed_call_is_no_longer_awaiting(awaiting) -> None:
    """``prepare_input`` 接收决定后占位是 running：执行期间再提交被拒，原调用不会被重放第二次。"""
    awaiting.crud.replace_message(
        awaiting.task_id,
        awaiting.placeholder.sequence,
        ConversationTaskContextRecord(
            task_id=awaiting.task_id,
            run_id=awaiting.placeholder.run_id,
            message=awaiting.placeholder.message,
            include_in_context=True,
            sequence=awaiting.placeholder.sequence,
            metadata=ToolMessageMetadata(status="running"),
        ),
    )

    with pytest.raises(SessionConflictError) as caught:
        module.ToolDecisionService().submit(awaiting.task_id, _CALL_ID, "approve")

    assert caught.value.code == "DECISION_NOT_AWAITED"
    assert _queued_decisions(awaiting.task_id) == []


def test_concurrent_submissions_enqueue_exactly_one(awaiting, monkeypatch: pytest.MonkeyPatch) -> None:
    """用户交还与运行时代为落定同时提交：判定与入队在一个写事务里，后到的一方看到已提交。"""
    queue = depends.get_task_message_queue()
    original = queue.pending_nowait
    first_checked = threading.Event()

    def slow_first_check(task_id: int, **kwargs):
        pending = original(task_id, **kwargs)
        if not first_checked.is_set():
            # 第一方判定完、入队前停一下，让第二方在这个窗口里判定。
            first_checked.set()
            time.sleep(0.3)
        return pending

    monkeypatch.setattr(queue, "pending_nowait", slow_first_check)
    outcomes: list[str] = []

    def submit(decision: str) -> None:
        try:
            module.ToolDecisionService().submit(awaiting.task_id, _CALL_ID, decision, source="user")  # type: ignore[arg-type]
            outcomes.append("enqueued")
        except SessionConflictError as exc:
            outcomes.append(exc.code)

    first = threading.Thread(target=submit, args=("approve",))
    first.start()
    assert first_checked.wait(2)
    second = threading.Thread(target=submit, args=("reject",))
    second.start()
    first.join(5)
    second.join(5)

    assert sorted(outcomes) == ["DECISION_ALREADY_SUBMITTED", "enqueued"]
    [queued] = _queued_decisions(awaiting.task_id)
    assert queued.decision == "approve"
