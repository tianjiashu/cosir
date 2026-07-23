"""Payload model for run_failed events."""

from typing import Literal

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class RunFailedPayload(RuntimeEventPayload):
    """运行失败事件 payload。"""

    error: str
    status: Literal["failed"] | None = None
    message: str | None = None
    step_id: str | None = None
    requested_agent_id: str | None = None
    task_agent_id: str | None = None
    tool_name: str | None = None
