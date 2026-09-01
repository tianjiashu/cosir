from collections.abc import Mapping

from app.models.enums.workspace_event_type import WorkspaceEventType
from app.models.payload import (
    WorkspaceDegradedPayload,
    WorkspacePreparingPayload,
    WorkspaceReadyPayload,
)
from app.models.payload.workspace_payload.workspace_payload_base import WorkspacePayloadBase

EVENT_PAYLOAD_MODELS: Mapping[WorkspaceEventType, type[WorkspacePayloadBase]] = {
    WorkspaceEventType.PREPARING: WorkspacePreparingPayload,
    WorkspaceEventType.READY: WorkspaceReadyPayload,
    WorkspaceEventType.DEGRADED: WorkspaceDegradedPayload,
}
