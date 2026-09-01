"""Workspace readiness notification types."""

from enum import Enum


class WorkspaceEventType(str, Enum):
    """Stable event names for workspace preparation notifications."""

    PREPARING = "workspace_preparing"
    READY = "workspace_ready"
    DEGRADED = "workspace_degraded"

    def __str__(self) -> str:
        """Return the stable wire value."""

        return self.value
