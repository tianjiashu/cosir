"""Startup recovery for Run-scoped terminal checkpoint metadata."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.core.workflows.react import workflow as workflow_module
from app.core.workflows.react.workflow import ReactLikeWorkflow


class _GraphStub:
    def __init__(self) -> None:
        self.values = {
            "terminal_sessions": {
                "session-1": {
                    "session_id": "session-1",
                    "status": "running",
                    "next_seq": 7,
                },
                "session-2": {
                    "session_id": "session-2",
                    "status": "closed",
                    "end_reason": "terminal_close",
                },
            }
        }
        self.updated: dict[str, object] | None = None

    async def aget_state(self, _config: dict[str, object]) -> SimpleNamespace:
        return SimpleNamespace(values=self.values)

    async def aupdate_state(
        self, _config: dict[str, object], values: dict[str, object]
    ) -> None:
        self.updated = values
        self.values.update(values)


class _TerminalServiceStub:
    def __init__(self) -> None:
        self.closed: list[tuple[int, str]] = []

    def close_run_terminals(self, run_id: int, *, reason: str) -> None:
        self.closed.append((run_id, reason))


@pytest.mark.asyncio
async def test_startup_recovery_closes_active_terminal_checkpoint_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _GraphStub()
    terminal_service = _TerminalServiceStub()

    @asynccontextmanager
    async def checkpointer_context():
        yield object()

    monkeypatch.setattr(workflow_module, "build_checkpointer", checkpointer_context)
    monkeypatch.setattr(
        ReactLikeWorkflow,
        "_build_graph",
        lambda _self, _checkpointer: graph,
    )
    monkeypatch.setattr(
        workflow_module,
        "get_terminal_session_service",
        lambda: terminal_service,
    )

    count = await ReactLikeWorkflow().recover_orphaned_terminal_checkpoints(
        [SimpleNamespace(id=42, checkpoint_thread_id="thread-42")]
    )

    assert count == 1
    assert terminal_service.closed == [(42, "runtime_restarted")]
    assert graph.updated == {
        "terminal_sessions": {
            "session-1": {
                "session_id": "session-1",
                "status": "closed",
                "next_seq": 7,
                "end_reason": "runtime_restarted",
            },
            "session-2": {
                "session_id": "session-2",
                "status": "closed",
                "end_reason": "terminal_close",
            },
        }
    }
