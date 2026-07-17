"""运行时使用的模型适配器协议。"""

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Protocol

from app.tools.schemas import ToolCall


@dataclass(frozen=True)
class RuntimeMessage:
    """表示一个与模型无关的运行时消息。

    参数:
        role: 消息角色，例如 ``system``、``user``、``assistant`` 或 ``tool``。
        content_text: 纯文本消息内容。
        metadata: 用于追踪和后续扩展的可选结构化元数据。

    返回:
        一个运行时消息值对象。

    异常:
        无。

    副作用:
        无。
    """

    role: str
    content_text: str
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelDelta:
    """表示来自模型适配器的一个流式增量（delta）。

    参数:
        text: 该增量由模型产生的文本。
        is_final: 该增量是否标志着模型响应的结束。
        tool_call: 模型请求的可选工具调用。

    返回:
        一个模型增量值对象。

    异常:
        无。

    副作用:
        无。
    """

    text: str
    is_final: bool = False
    tool_call: Optional[ToolCall] = None


@dataclass(frozen=True)
class ModelToolDefinition:
    """描述一个面向模型的工具，去掉仅与执行相关的细节。

    参数:
        name: 对模型可见的稳定工具名。
        description: 对模型可见的、人类可读的工具描述。
        parameters_schema: 描述所接受参数的 JSON Schema 对象。

    返回:
        一个面向模型的工具定义值对象。

    异常:
        无。

    副作用:
        无。
    """

    name: str
    description: str
    parameters_schema: Mapping[str, Any]


class StreamingModelAdapter(Protocol):
    """定义运行时使用的流式模型适配器契约。"""

    async def stream(
        self,
        messages: List[RuntimeMessage],
        tools: Optional[List[ModelToolDefinition]] = None,
    ) -> AsyncIterator[ModelDelta]:
        """为运行时消息流式产出模型增量。

        参数:
            messages: 与模型无关的运行时消息。
            tools: 本次调用可用的、可选的面向模型的工具定义。

        生成:
            由服务商或本地适配器产生的 ModelDelta 值。

        异常:
            RuntimeError: 如果服务商请求或流处理失败。

        副作用:
            服务商实现可能执行网络 I/O。
        """

        ...
