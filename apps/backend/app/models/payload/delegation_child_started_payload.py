"""Payload model for delegation_child_started events."""

from typing import Literal

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class DelegationChildStartedPayload(RuntimeEventPayload):
    """Child turn start event payload for a delegation."""

    delegation_id: str
    parent_turn_id: str
    child_turn_id: str
    child_agent_id: str
    delegation_type: str
    status: Literal["running"]
