"""RuntimeMessage ↔ LangChain 消息 与 工具 schema 的边界转换。

本模块是 ``context/`` 模型无关层与 LangGraph 之间的唯一转换点：把运行时
``RuntimeMessage`` 转为 LangChain ``BaseMessage``、把 ``ToolDefinition`` 统一经 ``to_model_tool_definition()`` 投影后转为
``bind_tools`` 接受的 OpenAI 函数 schema、把 LangChain 的 ``tool_calls`` 还原为
内部 ``ToolCall``。除本模块外，graph 节点内部一律使用 LangChain 类型，不在
节点逻辑里散落转换代码。
"""

import json

from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.tool import ToolCall as LangChainToolCall

from app.models.runtime_message import RuntimeMessage
from app.tools.schemas import ToolCall, ToolDefinition


def runtime_to_langchain(messages: list[RuntimeMessage]) -> list[BaseMessage]:
    """将运行时消息转换为 LangChain 消息。

    参数:
        messages: 与模型无关的运行时消息列表。

    返回:
        可直接交给 LangChain chat model 的 ``BaseMessage`` 列表。

    异常:
        无。

    副作用:
        无。
    """

    converted: list[BaseMessage] = []
    for message in messages:
        if message.role == "system":
            converted.append(SystemMessage(content=message.content_text))
        elif message.role == "user":
            converted.append(HumanMessage(content=message.content_text))
        elif message.role == "assistant":
            tool_calls_meta = _tool_calls_from_metadata(message.metadata.get("tool_calls"))
            langchain_tool_calls = [
                {
                    "name": call["name"],
                    "args": call.get("args")
                    if isinstance(call.get("args"), dict)
                    else {},
                    "id": call.get("id") or "",
                }
                for call in tool_calls_meta
            ]
            converted.append(
                AIMessage(
                    content=message.content_text,
                    tool_calls=langchain_tool_calls,
                )
            )
        elif message.role == "tool":
            converted.append(
                ToolMessage(
                    content=message.content_text,
                    tool_call_id=message.metadata.get("tool_call_id", ""),
                )
            )
        else:
            converted.append(HumanMessage(content=message.content_text))
    return converted


def model_tools_to_langchain(
    tools: list[ToolDefinition],
) -> list[dict[str, Any]]:
    """将面向模型的工具定义转换为 ``bind_tools`` 接受的 OpenAI 函数 schema。

    统一经 ``ToolDefinition.to_model_tool_definition()`` 投影为模型可见结构，再投影为
    OpenAI 函数 schema（``{"name", "description", "parameters"}``）。直接返回内部函数 schema，
    由 LangChain 的 ``bind_tools`` 负责包装为 ``{"type": "function", "function": {...}}``，
    避免对具体包装格式的依赖，跨 langchain 版本更稳健。

    参数:
        tools: 内部工具定义列表。

    返回:
        ``bind_tools`` 可直接消费的 OpenAI 函数 schema 列表（``list[dict[str, Any]]``）。

    异常:
        无。

    副作用:
        无。
    """

    model_tools = [tool.to_model_tool_definition() for tool in tools]
    return [
        {
            "name": model_tool["name"],
            "description": model_tool["description"],
            "parameters": dict(model_tool["parameters_schema"]),
        }
        for model_tool in model_tools
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

    return [
        ToolCall(
            tool_name=call["name"],
            arguments=call.get("args") or {},
            call_id=call.get("id") or "",
        )
        for call in calls
    ]


def _tool_calls_from_metadata(raw: str | None) -> list[dict[str, Any]]:
    """从 ``RuntimeMessage.metadata`` 的 JSON 字符串还原 assistant 的 tool_calls。

    ``runner`` 侧把 langchain ``tool_calls`` 序列化为 JSON 字符串存入 ``metadata``，
    此处反序列化回 ``list[dict]`` 供 ``AIMessage`` 重建使用。

    参数:
        raw: ``metadata.get("tool_calls")`` 的 JSON 字符串，可能为空或非法。

    返回:
        tool_calls 字典列表；空串、非法 JSON 或非列表时返回空列表。

    异常:
        无。

    副作用:
        无。
    """

    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []
