"""ChildAgentRunner 从 canonical child run 事实读取委派结果。"""

from types import SimpleNamespace

import app.core.delegation.child_agent_runner as child_runner_module
from app.core.delegation.child_agent_runner import ChildAgentRunner


def _make_profile(run_id: int):
    return SimpleNamespace(run=SimpleNamespace(id=run_id))


def test_run_child_reads_completed_turn(monkeypatch) -> None:
    async def run_agent(_profile) -> None:
        return None

    monkeypatch.setattr(
        child_runner_module,
        "get_conversation_run_service",
        lambda: SimpleNamespace(
            get_run=lambda _id: SimpleNamespace(
                id=3, task_id=1, status="completed", end_reason=None
            )
        ),
    )
    monkeypatch.setattr(
        child_runner_module,
        "ConversationStateService",
        lambda: SimpleNamespace(
            build_run_state=lambda _task_id, _run_id: {
                "messages": [
                    {
                        "role": "assistant",
                        "parts": [{"type": "text", "text": "done"}],
                    }
                ]
            }
        ),
    )
    result = ChildAgentRunner(run_agent).run_child(_make_profile(3))
    assert result.status == "completed"
    assert result.summary == "done"


def test_run_child_reads_failed_turn(monkeypatch) -> None:
    async def run_agent(_profile) -> None:
        return None

    monkeypatch.setattr(
        child_runner_module,
        "get_conversation_run_service",
        lambda: SimpleNamespace(
            get_run=lambda _id: SimpleNamespace(
                status="failed", response_text=None, end_reason="max_steps_reached"
            )
        ),
    )
    result = ChildAgentRunner(run_agent).run_child(_make_profile(4))
    assert result.status == "failed"
    assert result.error == "max_steps_reached"


def test_run_child_respects_cancel_before_start() -> None:
    async def run_agent(_profile) -> None:
        raise AssertionError("cancelled child must not start")

    result = ChildAgentRunner(run_agent, should_cancel=lambda _id: True).run_child(_make_profile(5))
    assert result.status == "cancelled"
