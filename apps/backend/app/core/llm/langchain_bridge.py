"""RuntimeMessage ↔ LangChain 消息 与 工具 schema 的边界转换。

本模块是 ``context/`` 模型无关层与 LangGraph 之间的唯一转换点：把运行时
``RuntimeMessage`` 转为 LangChain ``BaseMessage``、把 ``ToolDefinition`` 统一经
``to_model_tool_definition()`` 投影后转为 ``bind_tools`` 接受的 OpenAI 函数 schema、
把 LangChain 的 ``tool_calls`` 还原为内部 ``ToolCall``。除本模块外，graph 节点内部
一律使用 LangChain 类型，不在节点逻辑里散落转换代码。
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

from app.config.logging.logger import log
from app.models.runtime_message import RuntimeMessage
from app.tools.schemas import ToolCall, ToolDefinition


def runtime_to_langchain(messages: list[RuntimeMessage]) -> list[BaseMessage]:
    """将运行时消息转换为 LangChain 消息。

    作为 ``RuntimeMessage`` ↔ LangChain 的唯一转换点，额外承担一道防御性清洗：
    先收集全部 ``tool`` 消息的 ``tool_call_id``，重建 ``assistant`` 的 ``AIMessage``
    时仅保留有对应 ``ToolMessage`` 配对的 ``tool_calls``，剥离无配对的悬空调用。
    历史脏数据（如旧版 ``tool_error_limit`` 分支曾丢弃本批次响应）会导致 assistant
    的 ``tool_calls`` 缺少对应 ``ToolMessage``，若直接提交 OpenAI 会触发协议校验失败
    （"assistant message with tool_calls must be followed by tool messages"），剥离
    可保证发往模型的消息序列始终闭合。

    参数:
        messages: 与模型无关的运行时消息列表。

    返回:
        可直接交给 LangChain chat model 的 ``BaseMessage`` 列表；其中 assistant 的
        ``tool_calls`` 已剔除无配对 ``ToolMessage`` 的悬空项。

    异常:
        无。

    副作用:
        当检测到悬空 ``tool_calls`` 被剥离时，经项目标准 ``log`` 单例写一条 ``warning``
        （事件 ``assistant_tool_calls_orphaned``），记录被剥离的 ``tool_call_id`` 列表以
        便追溯脏数据来源；不写入任何业务数据。
    """

    converted: list[BaseMessage] = []
    # 先收集所有 tool 消息的 tool_call_id 集合：assistant 的 tool_calls 若没有对应
    # ToolMessage 配对，提交给 OpenAI 会触发协议校验失败（"assistant message with
    # tool_calls must be followed by tool messages"）。历史脏数据（如旧版 tool_error_limit
    # 分支曾丢弃本批次响应）可能出现悬空 tool_calls，此处剥离无配对的调用，保证发往
    # 模型的消息序列始终闭合。
    responded_ids = {
        message.metadata.get("tool_call_id")
        for message in messages
        if message.role == "tool" and message.metadata.get("tool_call_id")
    }
    for message in messages:
        if message.role == "system":
            converted.append(SystemMessage(content=message.content_text))
        elif message.role == "user":
            converted.append(HumanMessage(content=message.content_text))
        elif message.role == "assistant":
            tool_calls_meta = _tool_calls_from_metadata(message.metadata.get("tool_calls"))
            orphan_calls = [
                call.get("id") or "<missing-id>"
                for call in tool_calls_meta
                if call.get("id") not in responded_ids
            ]
            if orphan_calls:
                # 历史脏数据导致 assistant 的 tool_calls 缺少对应 ToolMessage，已在本函数
                # 内剥离，避免提交 OpenAI 触发协议校验失败；记录以追溯脏数据来源。
                log.warning(
                    "assistant_tool_calls_orphaned",
                    extra={
                        "msg": (
                            f"检测到 {len(orphan_calls)} 个无配对 ToolMessage 的悬空 tool_calls，"
                            "已剥离以免触发 OpenAI 协议校验失败"
                        ),
                        "data": {"orphan_tool_call_ids": orphan_calls},
                    },
                )
            langchain_tool_calls = [
                {
                    "name": call["name"],
                    "args": call.get("args") if isinstance(call.get("args"), dict) else {},
                    "id": call.get("id") or "",
                }
                for call in tool_calls_meta
                if call.get("id") in responded_ids
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
