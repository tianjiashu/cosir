"""OpenAI SDK 请求边界：原生 chunk/快照和 client 生命周期。"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from typing import Any

from openai import AsyncOpenAI
from openai.lib.streaming.chat import ChatCompletionStreamState, ChunkEvent
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageParam,
    ChatCompletionStreamOptionsParam,
)
from openai.types.shared_params import FunctionDefinition, ResponseFormatJSONObject


def usage_from_response(value: Any) -> dict[str, Any] | None:
    """在 SDK 响应边界归一厂商用量；保留缓存与思考明细。"""
    if value is None:
        return None
    raw = value.model_dump()
    input_details = raw.get("prompt_tokens_details")
    output_details = raw.get("completion_tokens_details")
    cached = input_details.get("cached_tokens") if isinstance(input_details, dict) else None
    if cached is None:
        cached = raw.get("prompt_cache_hit_tokens")
    reasoning = output_details.get("reasoning_tokens") if isinstance(output_details, dict) else None
    if (
        all(raw.get(key) is None for key in ("prompt_tokens", "completion_tokens", "total_tokens"))
        and cached is None
        and reasoning is None
    ):
        return None
    # 只映射 provider 已给出的值；缺失不是零用量，数值有效性统一由 usage stats 判定。
    return {
        "input_tokens": raw.get("prompt_tokens"),
        "output_tokens": raw.get("completion_tokens"),
        "total_tokens": raw.get("total_tokens"),
        "input_token_details": {"cache_read": cached} if cached is not None else {},
        "output_token_details": {"reasoning": reasoning},
    }


def _tool_parameters(tools: Sequence[FunctionDefinition]) -> dict[str, Any]:
    """请求里的工具字段；没有工具时整段不带（部分厂商拒绝空 ``tools`` 或孤立的 ``parallel_tool_calls``）。"""
    if not tools:
        return {}
    return {
        "tools": [{"type": "function", "function": schema} for schema in tools],
        "parallel_tool_calls": True,
    }


class OpenAIChatModel:
    """每次驱动独占 client 与模型参数；流式推理和最终格式化共用连接。

    工具定义不绑在实例上，由调用方每次请求传入：一次驱动内的工具集可以变（见 ``RequestTools``）。
    """

    def __init__(self, client: AsyncOpenAI, parameters: dict[str, Any]) -> None:
        self.client = client
        self.parameters = dict(parameters)

    async def stream(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        *,
        tools: Sequence[FunctionDefinition],
    ) -> AsyncGenerator[ChunkEvent, None]:
        """流式推理，产出 SDK chunk 事件与聚合快照；``tools`` 是这一次请求的工具定义，顺序即请求顺序。

        SDK 内部快照可变；产出前复制，使调用方可以跨迭代保留快照。不启用 SDK 自动
        结构化解析；调用身份的规范化归 context 的持久化入口，完整工具调用由业务准入处理。
        """
        state = ChatCompletionStreamState()
        stream = await self.client.chat.completions.create(
            **self.parameters,
            **_tool_parameters(tools),
            messages=list(messages),
            stream=True,
            stream_options=ChatCompletionStreamOptionsParam(include_usage=True),
        )
        async with stream:
            async for chunk in stream:
                for event in state.handle_chunk(chunk):
                    # SDK 会从同一分片派生 chunk、content.delta、工具参数 delta/done 等事件。
                    # 下游只需 ChunkEvent 的原始增量和累计快照，筛掉其他事件以免重复消费。
                    if event.type == "chunk":
                        yield event.model_copy(
                            update={"snapshot": event.snapshot.model_copy(deep=True)}
                        )

    async def complete(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        *,
        tools: Sequence[FunctionDefinition],
        response_format: ResponseFormatJSONObject,
    ) -> ChatCompletion:
        """一次性生成最终格式化结果，直接返回 SDK 响应，不经过流式聚合。

        调用方传与主线请求相同的 ``tools`` 以接上缓存前缀，这里用 ``tool_choice`` 禁止调用（没有工具时
        不带它）。完成原因和 JSON/schema 校验仍由业务节点负责，不调用 SDK 自动解析 helper。
        """
        tool_parameters = _tool_parameters(tools)
        if tool_parameters:
            tool_parameters["tool_choice"] = "none"
        return await self.client.chat.completions.create(
            **self.parameters,
            **tool_parameters,
            messages=list(messages),
            stream=False,
            response_format=response_format,
        )

    async def close(self) -> None:
        """释放连接池，等待用户期间不持有网络资源。"""
        await self.client.close()
