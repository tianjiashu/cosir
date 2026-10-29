"""委派子 Agent 接入延迟结果：追加消息按是否并入运行中的子 Run 决定登记，投递失败收回登记。

等子 Task 落定、读结论的协程见 ``test_child_agent_results``；这里只记下它是否被排上事件循环。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import configuration
from app.core.agents.agent_profile import AgentProfileType
from app.core.tool.background.deferred_registry import deferred_registry
from app.core.tool.handler import child_agent_send
from app.core.tool.handler.child_agent import child_run_job
from app.core.tool.schemas import ToolExecutionContext
from app.models.conversation_run_options import ConversationRunOptions

_PARENT = 7
_CHILD = 11


@pytest.fixture(autouse=True)
def _clean_entries():
    yield
    deferred_registry.drop_task(_PARENT)


def _send(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    child_status: str,
    fail=False,
):
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
    scheduled: list[Any] = []

    def schedule(coroutine: Any, _loop: Any) -> Any:
        coroutine.close()
        scheduled.append(coroutine)
        return SimpleNamespace(cancel=lambda: True)

    monkeypatch.setattr(child_run_job.asyncio, "run_coroutine_threadsafe", schedule)
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
    return observation, prepared, scheduled


def test_send_merged_into_a_running_child_is_not_registered(monkeypatch, tmp_path) -> None:
    """并入运行中的 Run 不另登记、不另等：已有的等待读到的最新 Run 覆盖了这条消息。"""
    observation, prepared, scheduled = _send(monkeypatch, tmp_path, child_status="running")

    assert observation.status == "success"
    assert '"status":"merged"' in observation.content
    assert observation.deferred is False
    assert deferred_registry.pending(_PARENT) == []
    assert scheduled == []
    [kwargs] = prepared
    assert kwargs["delivery"] == "immediate"


def test_send_that_starts_a_new_run_is_registered(monkeypatch, tmp_path) -> None:
    observation, prepared, scheduled = _send(monkeypatch, tmp_path, child_status="completed")

    assert '"status":"submitted"' in observation.content
    assert observation.deferred is True
    assert deferred_registry.pending(_PARENT) == [f"child_task:{_CHILD}"]
    assert len(scheduled) == 1
    [kwargs] = prepared
    assert kwargs["delivery"] == "next_run"


def test_failed_delivery_discards_the_registration(monkeypatch, tmp_path) -> None:
    observation, _, scheduled = _send(
        monkeypatch, tmp_path, child_status="completed", fail=True
    )

    assert observation.status == "error"
    assert deferred_registry.pending(_PARENT) == []
    # 没投递出去就不等：等待只能在投递之后开始。
    assert scheduled == []


@pytest.mark.usefixtures("isolated_storage")
def test_child_tools_always_run_in_the_background() -> None:
    """子 Agent 一律后台：参数里没有 background，也不声明可选的延迟结果能力。"""
    from app.core.tool.handler.child_agent.child_agent_send_args import ChildAgentSendArgs
    from app.core.tool.handler.child_agent.delegate_task_args import DelegateTaskArgs

    assert "background" not in DelegateTaskArgs.model_fields
    assert "background" not in ChildAgentSendArgs.model_fields
    assert child_agent_send.ChildAgentSendTool().to_definition().deferred is None
