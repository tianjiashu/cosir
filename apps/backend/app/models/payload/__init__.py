"""Workspace 事件 payload 模型。

本包只承载 Workspace 准备链路的事件载荷。Runtime 通用领域事件体系已按
``docs/plan/conversation-facts-runtime-event-removal-plan.md`` 阶段 D 删除：运行期事实
由 canonical conversation facts 承载，审计与诊断由结构化日志 / trace 承担，不再存在
``RuntimeEvent`` 或通用 payload 注册表。
"""

from app.models.payload.workspace_payload.workspace_degraded_payload import WorkspaceDegradedPayload
from app.models.payload.workspace_payload.workspace_payload_base import WorkspacePayloadBase
from app.models.payload.workspace_payload.workspace_preparing_payload import (
    WorkspacePreparingPayload,
)
from app.models.payload.workspace_payload.workspace_ready_payload import WorkspaceReadyPayload

__all__ = [
    "WorkspaceDegradedPayload",
    "WorkspacePayloadBase",
    "WorkspacePreparingPayload",
    "WorkspaceReadyPayload",
]
