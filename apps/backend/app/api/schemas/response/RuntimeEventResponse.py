from typing import Any

from pydantic import BaseModel


class RuntimeEventResponse(BaseModel):
    """回放用运行时事件响应模型（与持久化事件字典对齐）。

    字段对齐 ``RuntimeEventCrud.list_by_task`` / ``list_by_turn`` 返回的事件字典，
    即可直接序列化回前端 ``RuntimeEvent``（思考 / 工具调用 / 状态变更）的回放帧。

    参数:
        event_id: 唯一事件标识。
        event_type: 稳定的机器可读事件类型字符串。
        task_id: 关联任务标识。
        turn_id: 关联轮次标识。
        sequence: 事件在单次运行流内的排序号。
        payload: 与 ``event_type`` 匹配的事件负载字典。
        created_at: 事件创建时间的 UTC 文本。
        message_id: 可选用户可读消息标识。
        tool_call_id: 可选工具调用标识。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    event_id: str
    event_type: str
    task_id: int
    turn_id: int | None = None
    sequence: int
    payload: dict[str, Any]
    created_at: str
    message_id: str | None = None
    tool_call_id: str | None = None

    @classmethod
    def from_event_dict(cls, event_dict: dict[str, Any]) -> "RuntimeEventResponse":
        """从持久化事件字典构造回放响应模型。

        把存储层返回的事件字典前向映射为 API 响应模型，消除重复的字段拆解逻辑，
        并兼容 ``message_id`` / ``tool_call_id`` 缺省的情况。

        参数:
            event_dict: 持久化事件字典（来自 ``RuntimeEventCrud`` 查询）。

        返回:
            与字典字段对齐的 ``RuntimeEventResponse`` 实例。

        异常:
            无。

        副作用:
            无。
        """

        return cls(
            event_id=event_dict["event_id"],
            event_type=event_dict["event_type"],
            task_id=event_dict["task_id"],
            turn_id=event_dict["turn_id"],
            sequence=event_dict["sequence"],
            payload=event_dict["payload"],
            created_at=event_dict["created_at"],
            message_id=event_dict.get("message_id"),
            tool_call_id=event_dict.get("tool_call_id"),
        )
