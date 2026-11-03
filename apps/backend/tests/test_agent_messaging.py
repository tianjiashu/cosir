"""send_message 的方向与回复：向上投递、提问后等回复、上级的发送交付给在等的提问、无关 Task 被拒。

向下的并入 / 新开 Run 见 ``test_child_agent_background``；这里用替身覆盖派生关系两端的编排。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.tool.background.registry import background_registry
from app.core.tool.multi_agent import send_message
from app.core.tool.schemas import ToolExecutionContext
from app.models.conversation_run_options import ConversationRunOptions
from app.models.task_record import TaskRecord

_PARENT = 7
_CHILD = 11


def _task(task_id: int, **fields: Any) -> TaskRecord:
    defaults: dict[str, Any] = {
        "id": task_id,
        "workspace_id": 1,
        "title": f"任务{task_id}",
        "current_run_id": task_id * 10,
        "task_type": "user",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    return TaskRecord(**{**defaults, **fields})


_TASKS = {
    _PARENT: _task(_PARENT),
    _CHILD: _task(_CHILD, task_type="delegate_task", parent_task_id=_PARENT),
}


@pytest.fixture(autouse=True)
def _clean_entries():
    yield
    for task_id in (_PARENT, _CHILD):
        background_registry.drop_task(task_id)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(prepared=[], enqueued=[], delivered=[])
    monkeypatch.setattr(
        send_message, "get_task_service", lambda: SimpleNamespace(get_task=_TASKS.__getitem__)
    )
    monkeypatch.setattr(send_message, "get_workspace_service", lambda: None)
    monkeypatch.setattr(
        send_message,
        "get_conversation_run_ledger",
        lambda: SimpleNamespace(
            get_run=lambda run_id: SimpleNamespace(
                id=run_id,
                status="completed",
                agent_id="main_agent",
                model_config_id=3,
                options=ConversationRunOptions(reasoning_effort="high"),
            )
        ),
    )

    def prepare(text: str, **kwargs: Any) -> str:
        state.prepared.append({"text": text, **kwargs})
        return f"agent-message:{text}"

    monkeypatch.setattr(send_message, "prepare_agent_message", prepare)

    def enqueue(task_id: int, message: Any) -> None:
        # 登记表交付回复也经这里入队（background_result），按消息类型分开记。
        if isinstance(message, str):
            state.enqueued.append((task_id, message))
        else:
            state.delivered.append((task_id, message.body))

    monkeypatch.setattr(send_message.task_runtime_spaces, "enqueue", enqueue)
    return state


def _context(task_id: int, tool_call_id: str, tmp_path: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        step_id="step-1",
        task_id=task_id,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=task_id * 10,
        event_loop=SimpleNamespace(is_closed=lambda: False),  # type: ignore[arg-type]
        tool_call_id=tool_call_id,
    )


def _send(task_id: int, to: int, text: str, tmp_path: Path, **kwargs: Any):
    return send_message.SendMessageTool().execute(
        to, text, execution_context=_context(task_id, f"call-{task_id}-{text}", tmp_path), **kwargs
    )


def test_child_tells_its_parent_without_waiting(env, tmp_path) -> None:
    """向上告知：以 Agent 消息投给上级（沿用上级当前 Run 的配置），不登记等待。"""
    observation = _send(_CHILD, _PARENT, "根因找到了", tmp_path)

    assert observation.status == "success"
    assert '"status":"delivered"' in observation.content
    assert observation.background is None
    assert observation.display_data["direction"] == "up"
    assert env.enqueued == [(_PARENT, "agent-message:根因找到了")]
    [prepared] = env.prepared
    assert prepared["source"].task_id == _CHILD
    assert (prepared["agent_id"], prepared["model_config_id"]) == ("main_agent", 3)
    assert background_registry.pending(_CHILD) == []


def test_child_question_waits_and_the_parents_next_message_is_the_reply(env, tmp_path) -> None:
    """提问登记一条等回复；上级随后的发送交付给它（以 background_result 回到下级），不另投消息。"""
    asked = _send(_CHILD, _PARENT, "用方案 A 还是 B？", tmp_path, expect_reply=True)

    assert '"status":"awaiting_reply"' in asked.content
    assert asked.background is not None and asked.background.kind == "agent_reply"
    assert background_registry.pending(_CHILD) == [_TASKS[_PARENT].title]
    assert env.enqueued == [(_PARENT, "agent-message:用方案 A 还是 B？")]

    replied = _send(_PARENT, _CHILD, "用 A", tmp_path)

    assert '"status":"replied"' in replied.content
    assert replied.display_data["direction"] == "down"
    # 回复没有作为 Agent 消息再投一次，而是交付给提问那次调用。
    assert env.enqueued == [(_PARENT, "agent-message:用方案 A 还是 B？")]
    [(task_id, body)] = env.delivered
    assert task_id == _CHILD
    assert body.tool_name == "agent_reply"
    assert body.tool_call_id == f"call-{_CHILD}-用方案 A 还是 B？"
    assert "用 A" in body.tool_result
    assert background_registry.pending(_CHILD) == []


def test_tasks_outside_the_derivation_relation_are_refused(env, tmp_path, monkeypatch) -> None:
    """只认派生关系两端：Team 节点与主 Task、无关 Task 之间不能互发。"""
    node = _task(21, task_type="agent_team_node", parent_task_id=_PARENT)
    stranger = _task(22)
    monkeypatch.setitem(_TASKS, 21, node)
    monkeypatch.setitem(_TASKS, 22, stranger)

    for sender, target in ((21, _PARENT), (_PARENT, 21), (22, _PARENT)):
        observation = _send(sender, target, "hi", tmp_path)
        assert observation.status == "error"
    assert env.enqueued == []


@pytest.mark.parametrize(
    ("fields", "deriver"),
    [
        ({"task_type": "delegate_task", "parent_task_id": 7}, 7),
        ({"task_type": "fork", "extra": {"fork": {"source_task_id": 7, "created_by": "agent"}}}, 7),
        ({"task_type": "fork", "extra": {"fork": {"source_task_id": 7}}}, None),
        ({"task_type": "agent_team_node", "parent_task_id": 7}, None),
        ({"task_type": "user"}, None),
    ],
)
def test_deriver_is_only_the_agent_that_delegated_or_forked(fields, deriver) -> None:
    assert _task(30, **fields).deriver_id() == deriver
