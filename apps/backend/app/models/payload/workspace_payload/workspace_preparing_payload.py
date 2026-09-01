"""Payload model for workspace_preparing events."""

from app.models.payload.workspace_payload.workspace_payload_base import WorkspacePayloadBase


class WorkspacePreparingPayload(WorkspacePayloadBase):
    """workspace 索引准备开始事件 payload。"""

    workspace_path: str
