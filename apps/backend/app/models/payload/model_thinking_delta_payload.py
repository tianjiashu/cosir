"""Payload model for model_thinking_delta events."""

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class ModelThinkingDeltaPayload(RuntimeEventPayload):
    """模型思考增量事件 payload。"""

    step_id: str
    text: str
