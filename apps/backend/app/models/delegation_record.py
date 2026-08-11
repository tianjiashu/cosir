"""Persisted delegation state value object."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class DelegationRecord:
    """Represent one parent-to-child Agent delegation request."""

    delegation_id: str
    task_id: str
    parent_turn_id: str
    child_turn_id: str
    parent_agent_id: str
    child_agent_id: str
    delegation_type: str
    status: str
    prompt: str
    summary: str
    error: str
    effective_tools: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
