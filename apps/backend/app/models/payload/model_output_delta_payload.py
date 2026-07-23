"""Payload model for model_output_delta events."""

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class ModelOutputDeltaPayload(RuntimeEventPayload):
    """模型输出增量事件 payload。"""

    step_id: str
    text: str
