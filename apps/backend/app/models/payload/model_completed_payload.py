"""Payload model for model_completed events."""

from app.models.payload.model_tool_call_payload import ModelToolCallPayload
from app.models.payload.runtime_event_payload import RuntimeEventPayload


class ModelCompletedPayload(RuntimeEventPayload):
    """模型输出完成事件 payload。"""

    step_id: str
    text: str
    tool_calls: list[ModelToolCallPayload]
