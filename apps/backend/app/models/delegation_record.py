"""Legacy in-memory delegation projection used by snapshot compatibility tests.

This value object is deliberately detached from storage.  Child Agent persistence is owned by
``TaskRecord`` and ``ConversationRunRecord``; this DTO has no ORM conversion or database write
operation and must not be treated as a second delegation state machine.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DelegationRecord:
    """Represent the projection fields accepted by the legacy snapshot helper."""

    id: int | None
    task_id: int
    parent_run_id: int | None
    child_run_id: int | None
    parent_agent_id: str | None
    child_agent_id: str
    status: str
    prompt: str
    summary: str
    error: str
    effective_tools: tuple[str, ...]
    child_task_id: int | None
    created_at: object | None = None
    updated_at: object | None = None
