"""归一化工具执行结果值对象。"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ToolObservation:
    """表示一个归一化的工具执行结果。

    参数:
        tool_name: 被执行工具的名称。
        status: 执行状态，例如 ``success`` 或 ``error``。
        content: 返回给运行时的文本观测结果。
        error: 当 status 为 ``error`` 时的可选错误消息。

    返回:
        一个归一化的工具观测值对象。

    异常:
        无。

    副作用:
        无。
    """

    tool_name: str
    status: str
    content: str
    error: str = ""
    tool_call_id: str = ""
