"""Payload model for delegation_started events."""

from typing import Literal

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class DelegationStartedPayload(RuntimeEventPayload):
    """Delegation creation event payload."""

    delegation_id: int
    parent_turn_id: int
    child_turn_id: int | None = None
    child_agent_id: str
    status: Literal["pending"]
