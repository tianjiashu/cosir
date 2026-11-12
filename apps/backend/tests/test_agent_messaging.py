"""普通消息对父、子、同级 Task 均只投递一次；不代替委派回传。"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.tool.multi_agent import send_message
from app.core.tool.schemas import ToolExecutionContext
from app.models.conversation_run_options import ConversationRunOptions
from app.models.task_record import TaskRecord
from app.tools.tool_handler.terminal.command_results import command_results
from tests.task_extra_fixtures import fork_extra

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
    _CHILD: _task(_CHILD, task_type="agent_task", parent_task_id=_PARENT),
}


@pytest.fixture(autouse=True)
def _clean_entries():
    yield
    for task_id in (_PARENT, _CHILD):
        command_results.drop_task(task_id)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(prepared=[], enqueued=[], delivered=[])
    def current(task_id):
        if _TASKS[task_id].current_run_id is None:
            return None
        return SimpleNamespace(agent_id="main_agent", model_config_id="test/m3",
                               options=ConversationRunOptions(reasoning_effort="high"))
    monkeypatch.setattr(send_message, "get_task_service", lambda: SimpleNamespace(
        get_task=_TASKS.__getitem__, require_current_run=current,
    ))
    monkeypatch.setattr(send_message.task_runtime_spaces, "get_or_create",
                        lambda _: SimpleNamespace(operation=nullcontext))
    monkeypatch.setattr(send_message, "get_workspace_service", lambda: None)
    monkeypatch.setattr(send_message.SendMessageTool, "_child_profile_launch",
                        lambda self, target, run, ctx: send_message.RunLaunch.from_run(run))

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
    assert observation.display_data["task_id"] == _PARENT
    assert env.enqueued == [(_PARENT, "agent-message:根因找到了")]
    [prepared] = env.prepared
    assert prepared["source"].task_id == _CHILD
    assert (prepared["agent_id"], prepared["model_config_id"]) == ("main_agent", "test/m3")
    assert not command_results.live_background(_CHILD)


def test_question_and_explicit_reply_are_two_ordinary_messages(env, tmp_path) -> None:
    for sender, target, message in [(_CHILD, _PARENT, "A or B?"), (_PARENT, _CHILD, "A")]:
        sent = _send(sender, target, message, tmp_path)
        assert sent.background is None and '"status":"delivered"' in sent.content
        assert not command_results.live_background(sender)
    assert env.enqueued == [(_PARENT, "agent-message:A or B?"), (_CHILD, "agent-message:A")]
    assert not env.delivered


def test_peers_can_send_but_team_self_and_other_workspace_are_refused(env, tmp_path, monkeypatch) -> None:
    for task in (_task(21, task_type="team_node_task", parent_task_id=_PARENT),
                 _task(22), _task(23, workspace_id=2)):
        monkeypatch.setitem(_TASKS, task.id, task)
    assert _send(22, _CHILD, "news", tmp_path).status == "success"
    for sender, target in ((21, _PARENT), (_PARENT, 21), (_PARENT, _PARENT), (_PARENT, 23)):
        assert _send(sender, target, "hi", tmp_path).status == "error"
    assert env.enqueued == [(_CHILD, "agent-message:news")]


def test_target_without_first_run_inherits_its_queued_launch(env, tmp_path, monkeypatch):
    from app.core.workflow.message_queue.task_message import (
        AgentMessageBody,
        RunLaunch,
        TaskMessage,
    )
    from app.models.conversation_input import MessageSource

    target = _task(30, current_run_id=None)
    monkeypatch.setitem(_TASKS, target.id, target)
    pending = []
    monkeypatch.setattr(send_message, "get_task_message_queue",
                        lambda: SimpleNamespace(pending_nowait=lambda _: pending))
    assert _send(_PARENT, target.id, "hi", tmp_path).status == "error"
    assert not env.enqueued
    pending.append(TaskMessage.of(AgentMessageBody(
        text="first", launch=RunLaunch(model_config_id="test/m7"),
        source=MessageSource(task_id=_PARENT, name="parent"),
    )))
    assert _send(_PARENT, target.id, "hi", tmp_path).status == "success"
    assert env.prepared[0]["model_config_id"] == "test/m7"


@pytest.mark.parametrize(
    ("fields", "deriver"),
    [
        ({"task_type": "agent_task", "parent_task_id": 7}, 7),
        ({"task_type": "agent_fork", "extra": fork_extra(7)}, 7),
        ({"task_type": "user_fork", "extra": fork_extra(7)}, None),
        ({"task_type": "team_node_task", "parent_task_id": 7}, None),
        ({"task_type": "user"}, None),
    ],
)
def test_deriver_is_only_the_agent_that_delegated_or_forked(fields, deriver) -> None:
    assert _task(30, **fields).deriver_id() == deriver


def test_new_manual_fork_uses_frozen_launch_without_source_task(env, tmp_path, monkeypatch):
    target = _task(51, task_type="user_fork", current_run_id=None,
                   extra=fork_extra(987654, model_config_id="test/m8"))
    monkeypatch.setitem(_TASKS, 51, target)
    monkeypatch.setattr(send_message, "get_task_message_queue",
                        lambda: SimpleNamespace(pending_nowait=lambda _: []))
    assert _send(_PARENT, 51, "continue the branch", tmp_path).status == "success"
    assert env.prepared[-1]["model_config_id"] == "test/m8"
    assert env.prepared[-1]["reasoning_effort"] == "low"


def test_deleted_fork_model_is_rejected_before_enqueue(env, isolated_storage, tmp_path, monkeypatch):
    """Real model deletion + send_message preparation: a frozen ID never bypasses admission."""
    from app.core.tool.multi_agent.agent_message import prepare_agent_message
    from app.models.task_kind import TaskType
    from app.service.depends import get_model_config_service
    from app.storage.crud.task_crud import TaskCrud
    from app.storage.crud.workspace_crud import WorkspaceCrud
    from tests.model_config_fixtures import create_model_config

    model = create_model_config(name="fork", base_url="http://localhost", api_key="test",
                                model_name="test", context_window_k=8,
                                supports_reasoning_effort=True)
    workspace = WorkspaceCrud().create("review", str(tmp_path))
    target = TaskCrud().create(workspace.id, "branch", task_type=TaskType.USER_FORK,
                               extra=fork_extra(_PARENT, model_config_id=model.id))
    monkeypatch.setitem(_TASKS, target.id, target)
    monkeypatch.setattr(send_message, "get_task_message_queue",
                        lambda: SimpleNamespace(pending_nowait=lambda _: []))
    # Keep the real preparation and its single model-validation boundary.
    monkeypatch.setattr(send_message, "prepare_agent_message", prepare_agent_message)
    get_model_config_service().delete_connection(model.connection.id)
    result = _send(_PARENT, target.id, "continue", tmp_path)
    assert result.status == "error"
    assert "重新选择模型" in result.content
    assert not env.enqueued and not env.delivered
    assert TaskCrud().get(target.id).extra.fork.initial_launch.model_config_id == model.id
