"""Payload model for run_finished events."""

from typing import Literal

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class RunFinishedPayload(RuntimeEventPayload):
    """运行完成事件 payload。"""

    status: Literal["completed"]
    step_id: str | None = None
