"""Payload model for delegation_cancelled events."""

from typing import Literal

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class DelegationCancelledPayload(RuntimeEventPayload):
    """Cancelled delegation completion event payload."""

    delegation_id: int
    parent_turn_id: int
    child_turn_id: int
    child_agent_id: str
    status: Literal["cancelled"]
    error: str | None = None
