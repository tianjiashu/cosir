"""SDK 响应到请求正文的唯一投影；不承载消息身份、工具状态或业务恢复。"""

from __future__ import annotations

import json
from collections.abc import Collection
from typing import Any, cast

from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
    ParsedChatCompletionMessage,
)
from openai.types.chat.chat_completion_message_function_tool_call_param import Function
from pydantic import TypeAdapter

_message_adapter: TypeAdapter[ChatCompletionMessageParam] = TypeAdapter(ChatCompletionMessageParam)


def validate_message(value: Any) -> ChatCompletionMessageParam:
    """只在存储读取边界校验 SDK 消息；保留厂商扩展，消费惰性 Iterable 校验。"""
    parsed = _message_adapter.validate_python(value)
    _message_adapter.dump_python(parsed, mode="json", warnings="error")
    if value.get("reasoning_content") is not None and not isinstance(
        value["reasoning_content"], str
    ):
        raise ValueError("reasoning_content 必须为字符串")
    if value["role"] == "assistant" and any(
        call["type"] != "function" for call in value.get("tool_calls") or []
    ):
        raise ValueError("当前上下文只支持 function 工具调用")
    return cast(ChatCompletionMessageParam, value)


def assistant_message(
    message: ParsedChatCompletionMessage[object],
    registered_tool_names: Collection[str],
) -> ChatCompletionAssistantMessageParam:
    """只保留有 id 且工具名已注册的 function 调用；参数原样交给执行层解析。

    SDK 流式快照可能尚缺 id/name，过滤规则对草稿和最终提交相同。未知工具不进入
    模型上下文；已注册但本 Run 禁用的工具仍保留，由执行层给出拒绝观察。
    """
    result: ChatCompletionAssistantMessageParam = ChatCompletionAssistantMessageParam(
        role="assistant",
        content=message.content,
        refusal=message.refusal,
    )
    calls: list[ChatCompletionMessageFunctionToolCallParam] = []
    for call in message.tool_calls or []:
        call_id = call.id or ""
        function = getattr(call, "function", None)
        tool_name = getattr(function, "name", None) or ""
        if not call_id.strip() or tool_name not in registered_tool_names:
            continue
        calls.append(
            ChatCompletionMessageFunctionToolCallParam(
                id=call_id,
                type="function",
                function=Function(
                    name=tool_name,
                    # 原样保留模型产出的参数串（可能是不完整 JSON，如 ``{"path":``）：完整性由执行层
                    # ``decode_tool_arguments`` 判定并形成失败观察，投影层不补全、不重排、不解析。
                    # 缺失时留空串而不是 ``"{}"``，避免把残缺调用伪造成一次合法的空参数调用。
                    arguments=getattr(function, "arguments", None) or "",
                ),
            )
        )
    # 空列表不写入：``plan_tool_call_closures`` 以真值判断「本消息是否携带调用」，写空数组
    # 只会让库里的消息形状与语义不一致。
    if calls:
        result["tool_calls"] = calls
    reasoning = (message.model_extra or {}).get("reasoning_content")
    if isinstance(reasoning, str):
        result["reasoning_content"] = reasoning  # type: ignore[typeddict-unknown-key]
    return result


def _reject_json_constant(value: str) -> None:
    """SDK 工具参数必须为标准 JSON，不能把 NaN/Infinity 传入执行或 UI。"""
    raise ValueError(value)


def decode_tool_arguments(arguments: str) -> dict[str, Any]:
    """解码完整工具参数；格式不合法或顶层非对象时抛 ValueError，由边界决定拒绝方式。"""
    value = json.loads(arguments, parse_constant=_reject_json_constant)
    if not isinstance(value, dict):
        raise ValueError("arguments must be a JSON object")
    return value


def display_arguments(arguments: str) -> dict[str, Any]:
    """UI 以 raw_arguments 展示未完成/非法参数，不伪造可执行参数。"""
    try:
        return decode_tool_arguments(arguments)
    except ValueError:
        return {"raw_arguments": arguments}
