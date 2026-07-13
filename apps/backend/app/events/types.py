"""Agent 运行期间发出的、带有类型的运行时事件。"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict
from uuid import uuid4


@dataclass(frozen=True)
class RuntimeEvent:
    """表示一个由后端运行时发出的事件。

    参数:
        event_type: 稳定的、机器可读的事件类型。
        task_id: 与该事件关联的任务标识符。
        payload: 可序列化为 JSON 的事件载荷。
        event_id: 唯一的事件标识符。
        created_at: 事件创建时的 UTC 时间戳。

    返回:
        一个运行时事件值对象。

    异常:
        无。

    副作用:
        在缺省值被使用时生成 UUID 和时间戳。
    """

    event_type: str
    task_id: str
    payload: Dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        """将事件转换为可序列化为 JSON 的字典。

        参数:
            无。

        返回:
            可用于 HTTP 与 SSE 响应的字典表示。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "task_id": self.task_id,
            "created_at": self.created_at.isoformat(),
            "payload": self.payload,
        }
