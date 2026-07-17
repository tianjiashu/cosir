"""模型工具调用请求值对象。"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    """表示一个模型请求的工具调用。

    参数:
        tool_name: 被请求工具的名称。
        arguments: 来自模型的原始工具参数。合法调用应使用对象。
        call_id: 用于配对观测结果的可选服务商工具调用标识符。

    返回:
        一个工具调用值对象。

    异常:
        无。

    副作用:
        无。
    """

    tool_name: str
    arguments: Any = field(default_factory=dict)
    call_id: str = ""
