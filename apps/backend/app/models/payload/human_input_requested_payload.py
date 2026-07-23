"""Payload model for reserved human_input_requested events."""

from typing import Any

from pydantic import Field

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class HumanInputRequestedPayload(RuntimeEventPayload):
    """请求用户输入事件 payload。"""

    prompt: str
    request_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
