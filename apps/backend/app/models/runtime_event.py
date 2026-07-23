from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.models.enums.event_type import EventType


@dataclass(frozen=True)
class RuntimeEvent:
    """表示一个由后端运行时发出的事件。

    参数:
        event_type: 稳定的、机器可读的事件类型枚举成员。
        task_id: 与该事件关联的任务标识符。
        turn_id: 与该事件关联的轮次标识符。
        sequence: 事件在当前单次运行流内的排序号。当前尚未由存储层分配 task 级持久序号，
            外层 runtime 生命周期事件也可能保留默认值。
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

    ``payload`` 信封约定（多元展示兼容，当前真实 emit 多数尚未使用这些展示字段）：
        - ``display_format``: ``"text"`` | ``"component"``，展示形态。
        - ``component_type``: ``"chart"`` | ``"diff"`` | ``"code"`` | ``"table"``
          | ``"approval_prompt"`` | ``None``，组件渲染类型。
        - ``title`` / ``summary``: 人读标题 / 一句话摘要（降级展示用）。
        - ``details``: 结构化、机器可读明细（如工具特定数据）。
        - ``arguments``: 工具入参展示。
        - ``_ext``: 各事件类型自由扩展字段的兜底命名空间。
        工具事件须容忍 ``tool_name`` / ``result`` 缺失（降级时为空），可读内容来自
        ``summary`` + ``details``。``to_dict`` 行为不变，仅固化上述约定。
    """

    event_type: EventType
    task_id: str
    turn_id: str | None = None
    sequence: int = 0
    message_id: str | None = None
    tool_call_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

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
