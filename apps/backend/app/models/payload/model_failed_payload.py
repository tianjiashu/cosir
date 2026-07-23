"""Payload model for reserved model_failed events."""

from typing import Literal

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class ModelFailedPayload(RuntimeEventPayload):
    """模型调用失败事件 payload。"""

    error: str
    step_id: str | None = None
    status: Literal["failed"] | None = None
