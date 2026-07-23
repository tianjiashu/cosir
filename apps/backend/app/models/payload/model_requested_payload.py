"""Payload model for model_requested events."""

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class ModelRequestedPayload(RuntimeEventPayload):
    """模型请求开始事件 payload。"""

    step_id: str
    message_count: int
