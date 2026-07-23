"""Payload model for reserved human_input_received events."""

from typing import Any

from pydantic import Field

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class HumanInputReceivedPayload(RuntimeEventPayload):
    """收到用户输入事件 payload。"""

    request_id: str | None = None
    response: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
