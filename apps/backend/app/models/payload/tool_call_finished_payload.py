"""Payload model for tool_call_finished events."""

from typing import Literal

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class ToolCallFinishedPayload(RuntimeEventPayload):
    """工具调用完成事件 payload。"""

    step_id: str
    tool_name: str
    status: Literal["success", "error"]
    tool_call_id: str
