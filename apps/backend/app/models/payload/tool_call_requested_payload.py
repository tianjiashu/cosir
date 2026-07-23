"""Payload model for reserved tool_call_requested events."""

from typing import Any

from pydantic import Field

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class ToolCallRequestedPayload(RuntimeEventPayload):
    """工具调用请求事件 payload。"""

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    step_id: str | None = None
    tool_call_id: str | None = None
