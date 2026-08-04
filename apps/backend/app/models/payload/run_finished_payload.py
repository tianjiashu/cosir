"""Payload model for run_finished events."""

from typing import Literal

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class RunFinishedPayload(RuntimeEventPayload):
    """运行完成事件 payload。"""

    status: Literal["completed"]
    step_id: str | None = None
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    reasoning_tokens: int = 0
    langfuse_trace_id: str | None = None
