"""测试使用真实 SDK 类型与聚合器，不复制生产消息实现。"""

from collections.abc import Iterable
from typing import Any

from openai.lib.streaming.chat import ChatCompletionStreamState, ChunkEvent
from openai.types import CompletionUsage
from openai.types.chat import ChatCompletion, ChatCompletionChunk, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from openai.types.completion_usage import CompletionTokensDetails, PromptTokensDetails


def sdk_completion(
    content: str | None = "",
    *,
    finish_reason: str | None = "stop",
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    reasoning_content: str | None = None,
) -> ChatCompletion:
    """构造完整 SDK 响应；参数使用 SDK 原生字段。"""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    return ChatCompletion.model_construct(
        id="completion-test",
        created=1,
        model="test",
        object="chat.completion",
        choices=[
            Choice.model_construct(
                index=0,
                finish_reason=finish_reason,
                message=ChatCompletionMessage.model_validate(message),
            )
        ],
        usage=CompletionUsage.model_construct(**usage) if usage is not None else None,
    )


def sdk_events(
    deltas: Iterable[dict[str, Any]],
    *,
    finish_reason: str | None = "stop",
    usage: dict[str, Any] | None = None,
) -> Iterable[ChunkEvent]:
    """用 SDK 聚合实际 delta，按网络顺序产出事件。"""
    state = ChatCompletionStreamState()
    frames = [
        {"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None}
        for delta in deltas
    ]
    frames.append({"index": 0, "delta": {}, "finish_reason": finish_reason})
    for choice in frames:
        chunk = ChatCompletionChunk.model_validate(
            {
                "id": "completion-test",
                "created": 1,
                "model": "test",
                "object": "chat.completion.chunk",
                "choices": [choice],
            }
        )
        for event in state.handle_chunk(chunk):
            if event.type == "chunk":
                yield event
    if usage is not None:
        chunk = ChatCompletionChunk.model_validate(
            {
                "id": "completion-test",
                "created": 1,
                "model": "test",
                "object": "chat.completion.chunk",
                "choices": [],
                "usage": usage,
            }
        )
        for event in state.handle_chunk(chunk):
            if event.type == "chunk":
                yield event


def sdk_event(delta: dict[str, Any], snapshot: ChatCompletion) -> ChunkEvent:
    """节点单测注入原生 SDK 事件；网络聚合由 MockTransport 集成测试覆盖。"""
    chunk = ChatCompletionChunk.model_validate(
        {
            "id": snapshot.id,
            "created": snapshot.created,
            "model": snapshot.model,
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta}],
        }
    )
    return ChunkEvent.model_construct(type="chunk", chunk=chunk, snapshot=snapshot)


def sdk_usage(stats: dict[str, Any] | None) -> dict[str, Any] | None:
    """用量测试把业务统计样例映射为 provider 字段，仍通过真实 SDK 响应消费。"""
    if not stats:
        return None
    details = stats.get("input_token_details")
    reasoning = stats.get("output_token_details")
    return {
        "prompt_tokens": stats.get("input_tokens", 0),
        "completion_tokens": stats.get("output_tokens", 0),
        "total_tokens": stats.get("total_tokens", 0),
        "prompt_tokens_details": PromptTokensDetails.model_construct(
            cached_tokens=details.get("cache_read")
        )
        if isinstance(details, dict)
        else details,
        "completion_tokens_details": CompletionTokensDetails.model_construct(
            reasoning_tokens=reasoning.get("reasoning")
        )
        if isinstance(reasoning, dict)
        else None,
    }
