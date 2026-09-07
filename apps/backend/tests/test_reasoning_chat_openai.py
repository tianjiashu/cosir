"""OpenAI-compatible reasoning chunk 适配器测试。"""

from typing import Any

from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

from app.core.llm_provider.capability.provider_capability import ProviderCapability
from app.core.llm_provider.reasoning_chat_openai import ReasoningChatOpenAI


def _convert(raw_chunk: dict[str, Any]) -> ChatGenerationChunk | None:
    """调用适配器的受保护转换入口，避免发起网络请求。"""

    model = ReasoningChatOpenAI(
        model="deepseek-v4-flash",
        api_key="test-key",
        base_url="https://api.deepseek.com",
    )
    return model._convert_chunk_to_generation_chunk(raw_chunk, AIMessageChunk, {})


def test_reasoning_content_is_preserved_in_streaming_chunk() -> None:
    """DeepSeek reasoning delta 应进入 LangChain chunk 的 additional_kwargs。"""

    generation = _convert(
        {
            "id": "chunk-1",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "先分析。",
                    },
                    "finish_reason": None,
                }
            ],
        }
    )

    assert generation is not None
    assert generation.message.additional_kwargs["reasoning_content"] == "先分析。"


def test_standard_content_and_tool_calls_still_use_parent_conversion() -> None:
    """普通文本和工具调用转换继续由 ChatOpenAI 父类负责。"""

    generation = _convert(
        {
            "id": "chunk-2",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "content": "完成。",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        }
    )

    assert generation is not None
    message = generation.message
    assert isinstance(message, AIMessageChunk)
    assert message.content == "完成。"
    assert message.tool_call_chunks[0]["id"] == "call-1"


def test_deepseek_capability_enables_thinking_request_body() -> None:
    """DeepSeek 能力配置应开启官方 thinking 请求参数。"""

    capability = ProviderCapability.get_capability("deepseek")

    assert capability.thinking_channel == "reasoning_content"
    assert capability.extra_body == {"thinking": {"type": "enabled"}}
