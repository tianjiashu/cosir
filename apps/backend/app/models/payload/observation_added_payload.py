"""Payload model for reserved observation_added events."""

from typing import Any, Literal

from pydantic import Field

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class ObservationAddedPayload(RuntimeEventPayload):
    """工具观察结果回填事件 payload。"""

    tool_name: str
    status: Literal["success", "error"]
    step_id: str | None = None
    tool_call_id: str | None = None
    summary: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
