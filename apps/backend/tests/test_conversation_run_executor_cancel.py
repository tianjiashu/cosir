import asyncio
from types import SimpleNamespace

import pytest

from app.assistant_transport.service.conversation_run_executor import ConversationRunExecutor


class _RunService:
    def __init__(self) -> None:
        self.run = SimpleNamespace(
            id=1,
            task_id=7,
            status="pending",
            end_reason=None,
        )

    def get_run(self, _run_id: int) -> SimpleNamespace:
        return self.run

    def claim_pending_run(self, _run_id: int) -> bool:
        return True

    def claim_or_resume_run(self, _run_id: int) -> bool:
        self.run.status = "running"
        return True

    def complete_run_if_running(self, _run_id: int) -> None:
        return None

    def cancel_run_if_running(
        self,
        _run_id: int,
        end_reason: str = "user_cancelled",
        final_output: str | None = None,
    ) -> SimpleNamespace | None:
        del final_output
        if self.run.status != "running":
            return None
        self.run.status = "cancelled"
        self.run.end_reason = end_reason
        return self.run

    def fail_run_if_running(
        self,
        _run_id: int,
        end_reason: str | None = None,
        final_output: str | None = None,
    ) -> None:
        del end_reason, final_output
        return None


class _Signal:
    def mark_cancelled(self, _run_id: int) -> None:
        return None

    def clear(self, _run_id: int) -> None:
        return None


@pytest.mark.asyncio
async def test_user_cancel_projects_tool_settlement_and_returns_normally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _RunService()
    executor = ConversationRunExecutor.__new__(ConversationRunExecutor)
    executor._run_service = service
    executor._persist_status = True
    executor._event_projector = None
    executor._signal = _Signal()
    executor._executions = {}
    executor._cancelling_run_ids = set()
    executor._cancellation_cleanup_tasks = set()
    projected: list[tuple[int, str, str]] = []
    monkeypatch.setattr(
        executor,
        "_project_tools_settled",
        lambda run_id, status, reason: projected.append((run_id, status, reason)),
    )

    async def runner(_run: object) -> None:
        await asyncio.Event().wait()

    await executor.start(1, runner)
    for _ in range(20):
        await asyncio.sleep(0)
        if service.run.status == "running":
            break

    assert service.run.status == "running"
    assert await executor.cancel(1) is True
    assert projected == [(1, "cancelled", "user_cancelled")]
    assert service.run.status == "cancelled"
    assert executor.is_cancelling(1) is False
