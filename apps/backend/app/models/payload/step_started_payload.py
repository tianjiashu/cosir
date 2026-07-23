"""Payload model for step_started events."""

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class StepStartedPayload(RuntimeEventPayload):
    """步骤开始事件 payload。"""

    step_id: str
    kind: str
    index: int
