"""Workspace 事件 payload registry。

Runtime 通用 payload registry 已随 ``RuntimeEvent`` 体系一并删除；本包现在只导出
Workspace 准备的 payload 映射。
"""

from app.models.payload.registry.workspace_event_payload_registery import EVENT_PAYLOAD_MODELS

__all__ = ["EVENT_PAYLOAD_MODELS"]
