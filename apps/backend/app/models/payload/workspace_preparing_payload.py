"""Payload model for workspace_preparing events."""

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class WorkspacePreparingPayload(RuntimeEventPayload):
    """workspace 索引准备开始事件 payload。"""

    workspace_path: str
