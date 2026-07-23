"""Payload model for a model-requested tool call."""

from typing import Any

from pydantic import Field

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class ModelToolCallPayload(RuntimeEventPayload):
    """模型输出中的工具调用描述。"""

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str = ""
