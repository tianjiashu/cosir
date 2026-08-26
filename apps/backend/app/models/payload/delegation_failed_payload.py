"""Payload model for delegation_failed events."""

from typing import Literal

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class DelegationFailedPayload(RuntimeEventPayload):
    """Failed delegation completion event payload."""

    delegation_id: int
    parent_turn_id: int
    child_turn_id: int
    child_agent_id: str
    status: Literal["failed"]
    error: str | None = None
