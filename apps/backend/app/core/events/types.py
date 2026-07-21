"""Agent 运行期间发出的、带有类型的运行时事件。"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class EventType(str, Enum):
    """运行时事件类型枚举。

    枚举成员的值即为 SSE ``event:`` 字段与持久化存储中的字符串，
    ``str(event_type)`` 会返回该稳定值，避免散落的字符串字面量产生拼写漂移。
    """

    RUN_STARTED = "run_started"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    RUN_FINISHED = "run_finished"
    STEP_STARTED = "step_started"
    MODEL_REQUESTED = "model_requested"
    MODEL_OUTPUT_DELTA = "model_output_delta"
    MODEL_COMPLETED = "model_completed"
    MODEL_FAILED = "model_failed"
    TOOL_CALL_REQUESTED = "tool_call_requested"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_FINISHED = "tool_call_finished"
    OBSERVATION_ADDED = "observation_added"
    FINAL_RESPONSE = "final_response"

    def __str__(self) -> str:
        """返回事件类型的稳定字符串值。

        参数:
            无。

        返回:
            可用于 SSE、日志与持久化边界的事件类型字面量。

        异常:
            无。

        副作用:
            无。
        """

        return self.value


@dataclass(frozen=True)
class RuntimeEvent:
    """表示一个由后端运行时发出的事件。

    参数:
        event_type: 稳定的、机器可读的事件类型枚举成员。
        task_id: 与该事件关联的任务标识符。
        turn_id: 与该事件关联的轮次标识符。
        sequence: 由存储层按 task 分配的稳定排序号。
        message_id: 可选的用户可读消息标识符。
        tool_call_id: 可选的工具调用标识符。
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
    turn_id: str | None = None
    sequence: int = 0
    message_id: str | None = None
    tool_call_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
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
            "turn_id": self.turn_id,
            "sequence": self.sequence,
            "message_id": self.message_id,
            "tool_call_id": self.tool_call_id,
            "created_at": self.created_at.isoformat(),
            "payload": self.payload,
        }
