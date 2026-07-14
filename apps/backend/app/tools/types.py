"""共享的工具值对象。"""

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence


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


@dataclass(frozen=True)
class ArtifactRequest:
    """描述需要由工具执行链统一落盘的文本产物。

    参数:
        kind: 产物类型。
        content: 需要写入的 UTF-8 文本。
        summary: 用于模型和 UI 的简短摘要。
        mime_type: 产物 MIME 类型。

    返回:
        不可变的产物创建请求。

    异常:
        无。

    副作用:
        无。实际写入只能由 ArtifactService 完成。
    """

    kind: str
    content: str
    summary: str
    mime_type: str = "text/plain"


@dataclass(frozen=True)
class ToolObservation:
    """表示一个归一化的工具执行结果。

    参数:
        tool_name: 被执行工具的名称。
        status: 执行状态，例如 ``success`` 或 ``error``。
        content: 返回给运行时的文本观测结果。
        error: 当 status 为 ``error`` 时的可选错误消息。
        permission: 与该工具关联的权限级别。
        approval_status: 该工具调用的审批决定状态。

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
    permission: str = ""
    approval_status: str = ""
    tool_call_id: str = ""
    artifact_id: Optional[str] = None
