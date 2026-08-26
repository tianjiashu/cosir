"""Workspace 状态事件值对象（workspace 级，不带 task/turn 信封）。

单一职责：承载 workspace 级状态事件（如创建时的准备进度 preparing/ready/degraded，
后续可扩展其他 workspace 状态事件）。与 ``RuntimeEvent`` 不同，本事件**无 task_id /
turn_id**（创建 workspace 时既无 task 也无 turn），自带 ``workspace_id`` 与
``workspace_path``，经独立的 ``WorkspaceEventBus`` 分发，供 workspace 级 SSE 端点
推送给前端状态展示。

属 ``models`` 层 leaf：仅依赖 ``EventType`` 枚举与标准库，零 ``app.*`` 编排依赖。
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from app.models.enums.event_type import EventType


@dataclass(frozen=True)
class WorkspaceEvent:
    """workspace 状态事件（创建时触发，后续可扩展其他 workspace 事件）。

    参数:
        event_type: 稳定事件类型，限 WORKSPACE_PREPARING / WORKSPACE_READY / WORKSPACE_DEGRADED。
        workspace_id: 所属 workspace 标识。
        workspace_path: workspace 根路径。
        payload: 复用对应 ``Workspace*Payload`` 的 ``model_dump`` 结果。
        event_id: 唯一事件标识。
        created_at: 事件创建 UTC 时间戳。

    返回:
        一个 workspace 状态事件值对象。

    异常:
        无。

    副作用:
        无。
    """

    event_type: EventType
    workspace_id: int
    workspace_path: str
    payload: dict[str, object]
    event_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """校验事件类型为 workspace 状态事件类型。

        参数:
            无。

        返回:
            无。

        异常:
            ValueError: 当 ``event_type`` 不是 WORKSPACE_PREPARING / WORKSPACE_READY /
                WORKSPACE_DEGRADED 时抛出。

        副作用:
            无。
        """

        allowed = {
            EventType.WORKSPACE_PREPARING,
            EventType.WORKSPACE_READY,
            EventType.WORKSPACE_DEGRADED,
        }
        if self.event_type not in allowed:
            raise ValueError(f"event_type must be a workspace event type: {self.event_type}")

    def to_dict(self) -> dict[str, object]:
        """转换为可序列化为 JSON 的字典。

        参数:
            无。

        返回:
            可用于 SSE data 字段的字典表示（含 event_id/event_type/workspace_id/
            workspace_path/payload/created_at）。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "workspace_id": self.workspace_id,
            "workspace_path": self.workspace_path,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
        }
