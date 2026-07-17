"""工具定义值对象。"""

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class ToolDefinition:
    """描述一个已注册的工具。

    参数:
        name: 模型工具调用使用的稳定工具名。
        description: 用于提示词和 UI 的、人类可读的工具描述。
        permission: 该工具所需的权限级别。
        required_params: 执行前必须存在的参数名。
        handler: 以关键字参数执行该工具的可调用对象。
        parameters_schema: 用于校验和提示词的、类 JSON Schema 的参数模式。
        timeout_seconds: 调度器返回错误前的最大执行时间。

    返回:
        一个工具定义值对象。

    异常:
        无。

    副作用:
        无。
    """

    name: str
    description: str
    permission: str
    required_params: Iterable[str]
    handler: Callable[..., Any]
    parameters_schema: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 10.0
    risk_level: str = "low"
    visible_by_default: bool = True
    resource_keys: Sequence[str] = field(default_factory=tuple)
