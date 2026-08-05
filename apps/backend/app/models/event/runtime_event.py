from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from app.models.enums.event_type import EventType
from app.models.payload.runtime_event_payload import RuntimeEventPayload


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
        payload: 与 ``event_type`` 匹配的 payload 实体。
        event_id: 唯一的事件标识符。
        created_at: 事件创建时的 UTC 时间戳。

    返回:
        一个运行时事件值对象。

    异常:
        pydantic.ValidationError: 当 ``payload`` 不符合 ``event_type`` 对应的 payload
            模型时抛出。
        KeyError: 当 ``event_type`` 尚未登记 payload 模型时抛出。

    副作用:
        在缺省值被使用时生成 UUID 和时间戳。

    ``payload`` 契约:
        ``payload`` 必须是 ``app.models.payload`` 下的 Pydantic payload 实体，且会按
        ``event_type`` 校验是否匹配对应模型。当前 payload 模型禁止契约外字段；如需新增展示字段
        （例如 ``display_format``、``component_type``、``summary``、``details``），
        必须先补对应 payload model，再重新生成前端 TypeScript 类型。
    """

    event_type: EventType
    task_id: str
    payload: RuntimeEventPayload
    turn_id: str | None = None
    sequence: int = 0
    message_id: str | None = None
    tool_call_id: str | None = None
    event_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """校验运行时事件 payload 实体。

        参数:
            无。

        返回:
            无。

        异常:
            TypeError: 当 ``payload`` 不是 ``event_type`` 对应的 payload 实体时抛出。
            KeyError: 当 ``event_type`` 尚未登记 payload 模型时抛出。

        副作用:
            无。
        """

        from app.models.payload.registry import EVENT_PAYLOAD_MODELS

        payload_model = EVENT_PAYLOAD_MODELS[self.event_type]
        if not isinstance(self.payload, payload_model):
            raise TypeError(f"payload for {self.event_type.value} must be {payload_model.__name__}")

    def to_dict(self) -> dict[str, object]:
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
            "payload": self.payload.model_dump(mode="json", exclude_none=True),
        }
