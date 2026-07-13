"""OpenAI 兼容的流式聊天补全适配器。"""

import json
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional

from app.models.base import ModelDelta, ModelToolDefinition, RuntimeMessage
from app.models.openai_stream_parser import parse_openai_sse_line, OpenAIStreamState


@dataclass(frozen=True)
class OpenAICompatibleModelConfig:
    """配置一个 OpenAI 兼容的聊天补全模型。

    参数:
        base_url: 基础 API 地址，例如 ``https://api.deepseek.com``。
        api_key_env: 存放 API Key 的环境变量名。
        model: 服务商模型标识符。
        timeout_seconds: 模型请求的 HTTP 超时时间。

    返回:
        不可变的模型适配器配置。

    异常:
        ValueError: 如果运行时消息无法被序列化为服务商格式。

    副作用:
        无。
    """

    base_url: str
    api_key_env: str
    model: str
    timeout_seconds: float = 60.0


class OpenAICompatibleStreamingAdapter:
    """从 OpenAI 兼容的聊天补全 API 流式产出响应。"""

    def __init__(self, config: OpenAICompatibleModelConfig) -> None:
        """使用服务商配置初始化适配器。

        参数:
            config: OpenAI 兼容的服务商配置。

        返回:
            无。

        异常:
            无。

        副作用:
            为后续的模型调用保存配置。
        """

        self._config = config

    async def stream(
        self,
        messages: List[RuntimeMessage],
        tools: Optional[List[ModelToolDefinition]] = None,
    ) -> AsyncIterator[ModelDelta]:
        """从 OpenAI 兼容的服务商流式产出模型增量。

        参数:
            messages: 发送给服务商的运行时消息。
            tools: 暴露给服务商的可选的面向模型的工具定义。

        生成:
            从服务商流式分块中解析出的 ModelDelta 值。

        异常:
            ValueError: 如果运行时消息无法被序列化为服务商格式，
                或者服务商流式分块包含不受支持或非法的工具调用。
            RuntimeError: 如果 API Key 缺失，或服务商请求失败。

        副作用:
            向配置好的模型服务商执行出站的 HTTPS I/O。
        """

        api_key = os.environ.get(self._config.api_key_env)
        if not api_key:
            raise RuntimeError(f"missing API key environment variable: {self._config.api_key_env}")

        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx is required for OpenAI-compatible streaming") from exc

        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        request_payload = _build_request_payload(self._config.model, messages, tools)
        headers = {"Authorization": f"Bearer {api_key}"}
        state = OpenAIStreamState()

        async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
            async with client.stream(
                "POST",
                url,
                headers=headers,
                json=request_payload,
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise RuntimeError(
                        f"model request failed: status={response.status_code} body={body[:500]!r}"
                    )
                async for line in response.aiter_lines():
                    for delta in parse_openai_sse_line(line, state):
                        yield delta


def _to_provider_message(message: RuntimeMessage) -> Dict[str, Any]:
    """将内部的运行时消息转换为服务商聊天格式。

    参数:
        message: 待转换的运行时消息。

    返回:
        OpenAI 兼容的聊天消息字典。

    异常:
        ValueError: 如果工具观测缺少对应的服务商工具调用 id。

    副作用:
        无。
    """

    if message.role == "tool":
        tool_call_id = message.metadata.get("tool_call_id")
        if not tool_call_id:
            raise ValueError("tool message requires tool_call_id metadata")
        return {
            "role": "tool",
            "content": message.content_text,
            "tool_call_id": tool_call_id,
        }
    if message.role == "assistant" and message.metadata.get("tool_call_id"):
        return {
            "role": "assistant",
            "content": message.content_text or None,
            "tool_calls": [
                {
                    "id": message.metadata["tool_call_id"],
                    "type": "function",
                    "function": {
                        "name": message.metadata["tool_name"],
                        "arguments": message.metadata.get("tool_arguments_json") or "{}",
                    },
                }
            ],
        }
    return {"role": message.role, "content": message.content_text}


def _build_request_payload(
    model: str,
    messages: List[RuntimeMessage],
    tools: Optional[List[ModelToolDefinition]],
) -> dict:
    """构建 OpenAI 兼容的聊天补全请求载荷。

    参数:
        model: 服务商模型标识符。
        messages: 发送给服务商的运行时消息。
        tools: 模型可用的、可选的面向模型的工具定义。

    返回:
        可序列化为 JSON 的、发送给服务商的请求载荷。

    异常:
        ValueError: 如果运行时消息无法被序列化为服务商格式。

    副作用:
        无。
    """

    payload = {
        "model": model,
        "messages": [_to_provider_message(message) for message in messages],
        "stream": True,
    }
    if tools:
        payload["tools"] = [_to_provider_tool(tool) for tool in tools]
        payload["tool_choice"] = "auto"
        payload["parallel_tool_calls"] = False
    return payload


def _to_provider_tool(tool: ModelToolDefinition) -> Dict[str, dict]:
    """将一个面向模型的工具定义转换为 OpenAI 兼容格式。

    参数:
        tool: 暴露给服务商的、面向模型的工具定义。

    返回:
        一个函数型工具的 OpenAI 兼容 ``tools`` 条目。

    异常:
        无。

    副作用:
        无。
    """

    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.parameters_schema),
        },
    }
