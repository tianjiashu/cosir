"""Workspace readiness snapshot response."""

from datetime import datetime

from pydantic import BaseModel


class WorkspaceReadinessResponse(BaseModel):
    """工作区准备状态的持久化快照。"""

    workspace_id: int
    status: str
    reason: str | None = None
    action_taken: str
    files_changed: int
    duration_ms: int
    revision: int
    updated_at: datetime | None = None
