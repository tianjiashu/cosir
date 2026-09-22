"""Live Run finalization observer for Child Agent sessions."""

from __future__ import annotations

from typing import Protocol

from app.models import ConversationRunRecord


class RunFinalizationObserver(Protocol):
    """Explicit post-commit callback for canonical Run terminal transitions."""

    def on_run_finalized(self, record: ConversationRunRecord) -> None:
        """Observe a committed terminal Run without changing its canonical status."""
        ...


class ChildAgentRunFinalizationObserver:
    """Forward committed Run terminals to the Child Agent session coordinator."""

    def __init__(self, session_service: object) -> None:
        self._session_service = session_service

    def on_run_finalized(self, record: ConversationRunRecord) -> None:
        """Notify child waiters and parent finalization cleanup after commit."""

        from app.service.child_agent.child_agent_session_service import ChildAgentSessionService

        if not isinstance(self._session_service, ChildAgentSessionService):
            raise TypeError("ChildAgentRunFinalizationObserver requires ChildAgentSessionService")
        self._session_service.on_run_finalized(record)
