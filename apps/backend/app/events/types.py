"""Agent 运行期间发出的、带有类型的运行时事件。"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict
from uuid import uuid4


class EventType(str, Enum):
    """运行时事件类型枚举。

    枚举成员的值即为 SSE ``event:`` 字段与持久化存储中的字符串，
    因此可直接当作 ``str`` 使用，避免散落的字符串字面量产生拼写漂移。
    """

    RUN_STARTED = "run_started"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    RUN_FINISHED = "run_finished"
    STEP_STARTED = "step_started"
    MODEL_OUTPUT_DELTA = "model_output_delta"
    TOOL_CALL_REQUESTED = "tool_call_requested"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_FINISHED = "tool_call_finished"
    TOOL_APPROVAL_REQUIRED = "tool_approval_required"
    OBSERVATION_ADDED = "observation_added"
    CHECKPOINT_CREATED = "checkpoint_created"
    CHECKPOINT_FAILED = "checkpoint_failed"
    FINAL_RESPONSE = "final_response"


@dataclass(frozen=True)
class RuntimeEvent:
    """表示一个由后端运行时发出的事件。

    参数:
        event_type: 稳定的、机器可读的事件类型枚举成员。
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

    event_type: EventType
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
