"""Payload model for reserved tool_call_started events."""

from typing import Any

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class ToolCallStartedPayload(RuntimeEventPayload):
    """工具调用开始事件 payload。"""

    tool_name: str
    step_id: str | None = None
    tool_call_id: str | None = None
    arguments: dict[str, Any] | None = None
    display: dict[str, Any] | None = None
    request_summary: dict[str, Any] | None = None
