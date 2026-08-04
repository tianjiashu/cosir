"""Payload model for run_cancelled events."""

from typing import Literal

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class RunCancelledPayload(RuntimeEventPayload):
    """运行取消事件 payload。"""

    status: Literal["cancelled"]
    step_id: str | None = None
    error: str | None = None
    langfuse_trace_id: str | None = None
