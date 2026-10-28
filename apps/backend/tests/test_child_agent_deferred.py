"""委派子 Agent 接入延迟结果：追加消息按是否并入运行中的子 Run 决定登记，投递失败收回登记。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import configuration
from app.core.agents.agent_profile import AgentProfileType
from app.core.tool.handler import child_agent_send
from app.core.tool.schemas import ToolExecutionContext
from app.core.workflow.deferred.deferred_results import deferred_results
from app.models.conversation_run_options import ConversationRunOptions

_PARENT = 7
_CHILD = 11


@pytest.fixture(autouse=True)
def _clean_entries():
    yield
    deferred_results.drop_task(_PARENT)


def _send(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, child_status: str, fail=False):
    """以替身执行一次 child_agent_send，返回 (观察, 入队消息的 prepare 参数)。"""
    child_run = SimpleNamespace(
        id=21,
        status=child_status,
        agent_id="child_a",
        model_config_id=1,
        options=ConversationRunOptions(),
    )
    monkeypatch.setattr(
        child_agent_send,
        "get_task_service",
        lambda: SimpleNamespace(
            get_task=lambda _id: SimpleNamespace(
                is_child=True,
                parent_task_id=_PARENT,
                workspace_id=3,
                current_run_id=21,
                title="child",
            )
        ),
    )
    monkeypatch.setattr(
        child_agent_send,
        "get_workspace_service",
        lambda: SimpleNamespace(get_workspace=lambda _id: SimpleNamespace(root_path=str(tmp_path))),
    )
    monkeypatch.setattr(
        child_agent_send,
        "get_conversation_run_ledger",
        lambda: SimpleNamespace(get_run=lambda _id: child_run),
    )
    profile = SimpleNamespace(
        agent_type=AgentProfileType.CHILD, model_settings=None, model_config_id=1
    )
    monkeypatch.setattr(
        configuration,
        "get_agent_registry",
        lambda: SimpleNamespace(resolve=lambda *_a: profile),
    )
    prepared: list[dict[str, Any]] = []

    def prepare(*args: Any, **kwargs: Any) -> str:
        prepared.append(kwargs)
        return "message"

    monkeypatch.setattr(
        child_agent_send,
        "get_user_input_service",
        lambda: SimpleNamespace(prepare=prepare),
    )

    def enqueue(*_args: Any, **_kwargs: Any) -> int:
        if fail:
            raise RuntimeError("queue unavailable")
        return 1

    monkeypatch.setattr(child_agent_send.task_runtime_spaces, "enqueue", enqueue)
    context = ToolExecutionContext(
        step_id="step-1",
        task_id=_PARENT,
        workspace_id=3,
        workspace_root=tmp_path,
        run_id=12,
        event_loop=SimpleNamespace(is_closed=lambda: False),  # type: ignore[arg-type]
        tool_call_id="call-send",
    )
    observation = child_agent_send.ChildAgentSendTool().execute(
        _CHILD, "Continue.", execution_context=context
    )
    return observation, prepared


def test_send_merged_into_a_running_child_is_not_registered(monkeypatch, tmp_path) -> None:
    observation, prepared = _send(monkeypatch, tmp_path, child_status="running")

    assert observation.status == "success"
    assert '"status":"merged"' in observation.content
    assert observation.deferred is False
    assert deferred_results.pending(_PARENT) == []
    [kwargs] = prepared
    assert kwargs["delivery"] == "immediate"
    # 即使不登记也带上触发调用：子 Run 恰好先结束时，这条消息开出的新 Run 仍按它回投。
    assert kwargs["trigger_tool_call_id"] == "call-send"


def test_send_that_starts_a_new_run_is_registered(monkeypatch, tmp_path) -> None:
    observation, prepared = _send(monkeypatch, tmp_path, child_status="completed")

    assert '"status":"submitted"' in observation.content
    assert observation.deferred is True
    assert deferred_results.pending(_PARENT) == [f"child_task:{_CHILD}"]
    [kwargs] = prepared
    assert kwargs["delivery"] == "next_run"
    assert kwargs["trigger_tool_call_id"] == "call-send"


def test_failed_delivery_discards_the_registration(monkeypatch, tmp_path) -> None:
    observation, _ = _send(monkeypatch, tmp_path, child_status="completed", fail=True)

    assert observation.status == "error"
    assert deferred_results.pending(_PARENT) == []
