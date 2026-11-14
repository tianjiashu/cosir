"""fork_session 工具：沿用父 Run 的发起配置、以 Agent 消息投递、登记后台等待与冲突收口。

复制本身（切分点、发起 Run 落为 forked）见 ``test_session_fork.py``；这里只用替身验证工具的编排。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config.settings import Settings
from app.core.tool.multi_agent import fork_session as fork_module
from app.core.tool.schemas import ToolExecutionContext
from app.models.errors.session_fork_errors import SessionForkConflictError
from app.models.session_extra import SessionExtra


def _context(tmp_path: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        step_id="step-1",
        session_id=7,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=70,
        event_loop=SimpleNamespace(is_closed=lambda: False),  # type: ignore[arg-type]
        tool_call_id="call-fork",
    )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(Settings, "AGENT_MAX_DERIVATION_DEPTH", 2)
    state = SimpleNamespace(
        parent_depth=0, parent_root=None, forked=[], prepared=[], enqueued=[], conflict=None
    )

    def fork_for_agent(source_session_id: int, **kwargs: Any):
        if state.conflict is not None:
            raise state.conflict
        state.forked.append({"source_session_id": source_session_id, **kwargs})
        return SimpleNamespace(
            id=41,
            extra=SessionExtra(writable_root=kwargs["narrowed_root"] or state.parent_root),
        )

    monkeypatch.setattr(
        fork_module,
        "get_session_service",
        lambda: SimpleNamespace(
            get_session=lambda session_id: SimpleNamespace(
                id=session_id, title="主对话", derivation_depth=state.parent_depth,
                extra=SessionExtra(writable_root=state.parent_root),
            ),
            fork_for_agent=fork_for_agent,
        ),
    )
    monkeypatch.setattr(
        fork_module,
        "get_run_ledger",
        lambda: SimpleNamespace(
            get_run=lambda _run_id: SimpleNamespace(
                agent_id="main_agent",
                model_config_id="test/m3",
                reasoning_effort="high",
            )
        ),
    )

    def prepare(text: str, **kwargs: Any) -> str:
        state.prepared.append({"text": text, **kwargs})
        return "agent-message"

    monkeypatch.setattr(fork_module, "prepare_agent_message", prepare)
    monkeypatch.setattr(
        fork_module.session_runtime_spaces,
        "enqueue",
        lambda session_id, message: state.enqueued.append((session_id, message)),
    )
    return state


def test_fork_keeps_the_parents_identity_and_completes_once_assigned(env, tmp_path) -> None:
    """分叉是编排：建好分叉、投递完交接与任务说明就完成，不登记后台等待（结论由派生汇报交回）。"""
    observation = fork_module.ForkSessionTool().execute(
        title="前端部分", message="只做前端", execution_context=_context(tmp_path)
    )

    assert observation.status == "success" and observation.background is None
    assert '"status":"started"' in (observation.content or "")
    assert observation.display_data == {
        "kind": "fork-session-result", "title": "前端部分", "child_session_id": 41,
    }
    [forked] = env.forked
    assert forked == {
        "source_session_id": 7, "run_id": 70, "tool_call_id": "call-fork",
        "title": "前端部分", "derivation_depth": 1, "narrowed_root": None,
    }
    # 分叉不换身份：模型与推理强度都沿用发起它的 Run；任务说明以 Agent 消息投递。
    [prepared] = env.prepared
    assert prepared["text"] == "只做前端"
    assert prepared["source"].session_id == 7
    assert (prepared["agent_id"], prepared["model_config_id"]) == ("main_agent", "test/m3")
    assert prepared["reasoning_effort"] == "high"
    # 交接通知排在任务说明之前：写清身份与会话归属，模型读到会话时已经知道自己是谁。
    [(handover_session, handover), assignment] = env.enqueued
    assert assignment == (41, "agent-message")
    assert handover_session == 41 and handover.body.notice_kind == "fork_handover"
    assert "session 41" in handover.body.content and "session 7" in handover.body.content


def test_fork_narrows_or_inherits_the_writable_root(env, tmp_path) -> None:
    """可写根只能收窄：给出时按来源校验后交给分叉，不给即继承；分叉沿用来源的系统提示词，范围写在交接通知里。"""
    (tmp_path / "pkg" / "web").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    env.parent_root = "pkg"
    tool = fork_module.ForkSessionTool()

    def fork(writable_root: str | None):
        return tool.execute(
            title="x", message="y", writable_root=writable_root, execution_context=_context(tmp_path)
        )

    narrowed = fork("pkg/web")
    assert env.forked[-1]["narrowed_root"] == "pkg/web"
    assert narrowed.display_data["writable_root"] == "pkg/web"
    assert "Your writable root is pkg/web" in env.enqueued[-2][1].body.content

    inherited = fork(None)
    assert env.forked[-1]["narrowed_root"] is None
    assert inherited.display_data["writable_root"] == "pkg"
    assert "Your writable root is pkg " in env.enqueued[-2][1].body.content

    widened = fork("docs")
    assert widened.status == "error" and "only be narrowed" in (widened.content or "")
    assert len(env.forked) == 2


def test_fork_conflicts_become_tool_errors(env, tmp_path) -> None:
    env.conflict = SessionForkConflictError("WORKSPACE_BUSY", "workspace is busy")
    observation = fork_module.ForkSessionTool().execute(
        title="x", message="y", execution_context=_context(tmp_path)
    )

    assert observation.status == "error"
    assert "Retry in a moment" in observation.content
    assert observation.display_data == {"status_hint": "分叉失败"}


def test_fork_args_require_a_short_title_and_a_message() -> None:
    from pydantic import ValidationError

    from app.core.tool.multi_agent.args.fork_session_args import TITLE_MAX, ForkSessionArgs

    ForkSessionArgs(title="前端", message="做前端")
    with pytest.raises(ValidationError):
        ForkSessionArgs(title="x" * (TITLE_MAX + 1), message="y")
    with pytest.raises(ValidationError):
        ForkSessionArgs(title=" ", message="y")


def test_assignment_description_matches_the_inherited_boundary():
    from app.core.tool.multi_agent.args.fork_session_args import ForkSessionArgs

    """分叉看不到当前回合：参数描述不能再告诉模型「分叉已有完整对话，只说新东西」。"""
    description = ForkSessionArgs.model_fields["message"].description or ""
    assert "complete assignment" in description
    assert "not the current turn" in description
