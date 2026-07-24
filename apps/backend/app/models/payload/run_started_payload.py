"""Payload model for run_started events."""

from typing import Literal

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class RunStartedPayload(RuntimeEventPayload):
    """运行开始事件 payload。"""

    status: Literal["running"]
    agent_id: str
