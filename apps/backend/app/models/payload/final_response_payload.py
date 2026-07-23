"""Payload model for final_response events."""

from typing import Literal

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class FinalResponsePayload(RuntimeEventPayload):
    """最终回答事件 payload。"""

    text: str
    step_id: str
    status: Literal["completed"]
