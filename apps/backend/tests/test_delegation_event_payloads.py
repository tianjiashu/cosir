"""delegation runtime event payload tests."""

from app.models.enums.event_type import EventType
from app.models.payload.registry.runtime_event_payload_registry import EVENT_PAYLOAD_MODELS


def test_delegation_payloads_are_registered():
    for event_type in (
        EventType.DELEGATION_STARTED,
        EventType.DELEGATION_CHILD_STARTED,
        EventType.DELEGATION_FINISHED,
        EventType.DELEGATION_FAILED,
        EventType.DELEGATION_CANCELLED,
    ):
        assert event_type in EVENT_PAYLOAD_MODELS
