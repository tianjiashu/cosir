"""fork_task 工具：沿用父 Run 的发起配置、以 Agent 消息投递、登记后台等待与冲突收口。

复制本身（切分点、发起 Run 落为 forked）见 ``test_task_fork.py``；这里只用替身验证工具的编排。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config.settings import Settings
from app.core.tool.multi_agent import fork_task as fork_module
from app.core.tool.schemas import ToolExecutionContext
from app.models.conversation_run_options import ConversationRunOptions
from app.models.errors.task_fork_errors import TaskForkConflictError


def _context(tmp_path: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        step_id="step-1",
        task_id=7,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=70,
        event_loop=SimpleNamespace(is_closed=lambda: False),  # type: ignore[arg-type]
        tool_call_id="call-fork",
    )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(Settings, "AGENT_MAX_DERIVATION_DEPTH", 2)
    state = SimpleNamespace(parent_depth=0, forked=[], prepared=[], enqueued=[], conflict=None)

    def fork_for_agent(source_task_id: int, **kwargs: Any):
        if state.conflict is not None:
            raise state.conflict
        state.forked.append({"source_task_id": source_task_id, **kwargs})
        return SimpleNamespace(id=41)

    monkeypatch.setattr(
        fork_module,
        "get_task_service",
        lambda: SimpleNamespace(
            get_task=lambda task_id: SimpleNamespace(
                id=task_id, title="主对话", derivation_depth=state.parent_depth
            ),
            fork_for_agent=fork_for_agent,
        ),
    )
    monkeypatch.setattr(
        fork_module,
        "get_conversation_run_ledger",
        lambda: SimpleNamespace(
            get_run=lambda _run_id: SimpleNamespace(
                agent_id="main_agent",
                model_config_id=3,
                options=ConversationRunOptions(
                    reasoning_effort="high", disabled_tool_groups=["浏览器工具"]
                ),
            )
        ),
    )

    def prepare(text: str, **kwargs: Any) -> str:
        state.prepared.append({"text": text, **kwargs})
        return "agent-message"

    def run_child(execution_context, child_task_id, *, label, enqueue, placeholder):
        enqueue()
        observation = placeholder()
        state.run_child = (child_task_id, label)
        return observation

    monkeypatch.setattr(fork_module, "prepare_agent_message", prepare)
    monkeypatch.setattr(fork_module, "run_child", run_child)
    monkeypatch.setattr(
        fork_module.task_runtime_spaces,
        "enqueue",
        lambda task_id, message: state.enqueued.append((task_id, message)),
    )
    return state


def test_fork_keeps_the_parents_identity_and_waits_in_the_background(env, tmp_path) -> None:
    observation = fork_module.ForkTaskTool().execute(
        title="前端部分", message="只做前端", execution_context=_context(tmp_path)
    )

    assert observation.status == "success"
    assert observation.display_data == {
        "kind": "fork-task-result", "title": "前端部分", "child_task_id": 41,
    }
    [forked] = env.forked
    assert forked == {
        "source_task_id": 7, "run_id": 70, "tool_call_id": "call-fork",
        "title": "前端部分", "derivation_depth": 1,
    }
    # 分叉不换身份：模型、推理强度与关掉的工具组都沿用发起它的 Run；任务说明以 Agent 消息投递。
    [prepared] = env.prepared
    assert prepared["text"] == "只做前端"
    assert prepared["source"].task_id == 7
    assert (prepared["agent_id"], prepared["model_config_id"]) == ("main_agent", 3)
    assert prepared["reasoning_effort"] == "high"
    assert prepared["disabled_tool_groups"] == ["浏览器工具"]
    # 交接通知排在任务说明之前：写清身份与任务归属，模型读到任务时已经知道自己是谁。
    [(handover_task, handover), assignment] = env.enqueued
    assert assignment == (41, "agent-message")
    assert handover_task == 41 and handover.body.notice_kind == "fork_handover"
    assert "task 41" in handover.body.content and "task 7" in handover.body.content
    assert env.run_child == (41, "前端部分")


def test_fork_conflicts_become_tool_errors(env, tmp_path) -> None:
    env.conflict = TaskForkConflictError("WORKSPACE_BUSY", "workspace is busy")
    observation = fork_module.ForkTaskTool().execute(
        title="x", message="y", execution_context=_context(tmp_path)
    )

    assert observation.status == "error"
    assert "Retry in a moment" in observation.content
    assert observation.display_data == {"status_hint": "分叉失败"}


def test_fork_args_require_a_short_title_and_a_message() -> None:
    from pydantic import ValidationError

    from app.core.tool.multi_agent.args.fork_task_args import TITLE_MAX, ForkTaskArgs

    ForkTaskArgs(title="前端", message="做前端")
    with pytest.raises(ValidationError):
        ForkTaskArgs(title="x" * (TITLE_MAX + 1), message="y")
    with pytest.raises(ValidationError):
        ForkTaskArgs(title=" ", message="y")


def test_assignment_description_matches_the_inherited_boundary():
    from app.core.tool.multi_agent.args.fork_task_args import ForkTaskArgs

    """分叉看不到当前回合：参数描述不能再告诉模型「分叉已有完整对话，只说新东西」。"""
    description = ForkTaskArgs.model_fields["message"].description or ""
    assert "complete assignment" in description
    assert "not the current turn" in description
