from collections.abc import Mapping

from app.models.enums.event_type import EventType
from app.models.payload import (
    RuntimeEventPayload,
    WorkspaceDegradedPayload,
    WorkspacePreparingPayload,
    WorkspaceReadyPayload,
)

EVENT_PAYLOAD_MODELS: Mapping[EventType, type[RuntimeEventPayload]] = {
    EventType.WORKSPACE_PREPARING: WorkspacePreparingPayload,
    EventType.WORKSPACE_READY: WorkspaceReadyPayload,
    EventType.WORKSPACE_DEGRADED: WorkspaceDegradedPayload,
}
