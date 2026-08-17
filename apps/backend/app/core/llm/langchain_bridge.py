"""LangChain 消息清洗 与 工具 schema 边界转换。

本模块是 LangGraph 消息链路上的边界收口：清洗进入上下文的 assistant 消息
（``sanitize_assistant_messages``）、把内部 ``ToolDefinition`` 投影为 ``bind_tools``
接受的 OpenAI 函数 schema（``model_tools_to_langchain``）、把 LangChain 的
``tool_calls`` 还原为内部 ``ToolCall``（``tool_calls_from_langchain``）。
除本模块外，graph 节点内部一律使用 LangChain 类型，不在节点逻辑里散落转换代码。
"""

from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.messages.tool import ToolCall as LangChainToolCall

from app.tools.schemas import ToolCall, ToolDefinition

# DeepSeek 等 OpenAI 兼容端点在 assistant 消息携带 tool_calls 但 content 为空串时，
# litellm 的 OpenAI 兼容序列化层会把 content="" 强制改写为 null，而 DeepSeek 拒绝
# assistant.content 为 null（仅 tool 角色允许 content=null）。用单空格占位符兜底，
# 保证序列化后 content 为非空字符串，根除整类协议拒绝问题。
_ASSISTANT_EMPTY_CONTENT_PLACEHOLDER = " "


def sanitize_assistant_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    """在消息进入上下文（入口守卫）前对 assistant 消息做最终清洗，避免脏字段回灌下一轮对话。

    重建后只保留安全的 ``content`` + 合法 ``tool_calls`` + ``id``。``ToolMessage`` 及其他
    角色不受影响（其 content=null 协议允许），保持原对象引用。

    参数:
        messages: 即将进入上下文的 LangChain 消息列表。

    返回:
        清洗后的新列表；非 assistant 类消息保持原对象引用不变。

    异常:
        无。

    副作用:
        无（不修改入参对象；仅在需要清洗的 assistant 消息时新建对象）。
    """
    normalized: list[BaseMessage] = []
    for message in messages:
        if not isinstance(message, AIMessage | AIMessageChunk):
            normalized.append(message)
            continue

        content = message.content
        # content 可能是内容块列表（str | list），仅 str 才有 strip；非 str/空串一律走占位符。
        if not isinstance(content, str) or content.strip() == "":
            content = _ASSISTANT_EMPTY_CONTENT_PLACEHOLDER

        # 重建时不传 invalid_tool_calls / response_metadata：前者是当轮解析噪声，后者是本地元数据，
        # 两者均不应回灌下一轮对话。
        normalized.append(
            AIMessage(
                content=content,
                tool_calls=message.tool_calls,
                id=message.id,
            )
        )
    return normalized


def model_tools_to_langchain(
    tools: list[ToolDefinition],
    allowed_tools: set[str] | None = None,
) -> list[dict[str, Any]]:
    """将面向模型的工具定义转换为 ``bind_tools`` 接受的 OpenAI 函数 schema。

    统一经 ``ToolDefinition.to_model_tool_definition()`` 投影为模型可见结构（``{"name",
    "description", "parameters"}``），并仅保留 ``allowed_tools`` 中允许的工具。直接返回
    内部函数 schema 列表，由 LangChain 的 ``bind_tools`` 负责包装为 ``{"type": "function",
    "function": {...}}``，避免对具体包装格式的依赖，跨 langchain 版本更稳健。

    参数:
        tools: 内部工具定义列表。
        allowed_tools: 允许暴露给模型的工具名集合；传入 ``None`` 表示不过滤（保留全部工具）。

    返回:
        ``bind_tools`` 可直接消费的 OpenAI 函数 schema 列表（``list[dict[str, Any]]``）。

    异常:
        无。

    副作用:
        无。
    """

    return [
        tool.to_model_tool_definition()
        for tool in tools
        if allowed_tools is None or tool.name in allowed_tools
    ]


def tool_calls_from_langchain(calls: list[LangChainToolCall]) -> list[ToolCall]:
    """将 LangChain 的 ``tool_calls`` 还原为内部 ``ToolCall``。

    参数:
        calls: LangChain chat model 产出的 ``tool_calls`` 列表（每个含 name/args/id）。

    返回:
        内部工具调用列表，供工具执行层消费。

    异常:
        无。

    副作用:
        无。
    """
    if not calls or len(calls) == 0:
        return []

    return [
        ToolCall(
            tool_name=call["name"],
            arguments=call.get("args") or {},
            call_id=call.get("id") or "",
        )
        for call in calls
    ]
