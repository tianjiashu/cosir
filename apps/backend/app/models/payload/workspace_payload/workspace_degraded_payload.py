"""Payload model for workspace_degraded events."""

from app.models.payload.workspace_payload.workspace_payload_base import WorkspacePayloadBase


class WorkspaceDegradedPayload(WorkspacePayloadBase):
    """workspace 索引降级事件 payload（CodeGraph 不可用，放行 + 文件搜索兜底）。

    ``state`` 为降级来源（'failed' | 'unavailable'），``degraded_reason`` 为面向人/模型的英文摘要。
    """

    workspace_path: str
    state: str
    degraded_reason: str
