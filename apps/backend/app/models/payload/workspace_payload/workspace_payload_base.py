"""Base model for workspace readiness payloads."""

from pydantic import BaseModel, ConfigDict


class WorkspacePayloadBase(BaseModel):
    """Typed payload boundary for workspace-only notifications."""

    model_config = ConfigDict(extra="forbid")
