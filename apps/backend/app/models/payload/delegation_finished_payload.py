"""Payload model for delegation_finished events."""

from typing import Literal

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class DelegationFinishedPayload(RuntimeEventPayload):
    """Successful delegation completion event payload."""

    delegation_id: str
    parent_turn_id: str
    child_turn_id: str
    child_agent_id: str
    delegation_type: str
    status: Literal["completed"]
    summary: str | None = None
