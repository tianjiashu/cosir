"""真实 SDK + 本机 HTTP mock 覆盖消息边界与流式聚合。"""

import json

import httpx
import pytest
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from app.core.llm_provider.message_codec import assistant_message, validate_message
from app.core.llm_provider.openai_model import OpenAIChatModel, usage_from_response
from tests.sdk_fixtures import sdk_completion, sdk_events


def test_assistant_message_filters_unregistered_calls() -> None:
    """缺失 id 或未注册的工具名不进入模型正文。"""

    completed = sdk_completion(
        content="说明",
        tool_calls=[
            {"id": "ok", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            {
                "id": "ghost",
                "type": "function",
                "function": {"name": "ghost_tool", "arguments": "{}"},
            },
        ],
    )

    message = assistant_message(completed.choices[0].message, {"read_file"})

    assert [call["id"] for call in message["tool_calls"]] == ["ok"]


@pytest.mark.asyncio
async def test_complete_preserves_bound_tools_without_changing_stream_requests():
    """真实 SDK 请求同时验证工具前缀、非流式响应与单次格式化参数隔离。"""
    requests = []
    completion = {
        "id": "result-1",
        "object": "chat.completion",
        "created": 1,
        "model": "test",
        "choices": [
            {
                "index": 0,
                "finish_reason": "length",
                "message": {"role": "assistant", "content": '{"partial":'},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if not body["stream"]:
            return httpx.Response(200, json=completion)
        chunk = {
            "id": "stream-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "delta": {"role": "assistant", "content": "done"},
                }
            ],
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
        )

    schemas = [{"name": "read_file"}, {"name": "write_file"}]
    model = OpenAIChatModel(
        AsyncOpenAI(
            api_key="test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ),
        {"model": "test"},
        tools=schemas,
    )
    messages = [{"role": "user", "content": "go"}]
    try:
        assert [event async for event in model.stream(messages)]
        response = await model.complete(messages, response_format={"type": "json_object"})
        assert [event async for event in model.stream(messages)]
        assert isinstance(response, ChatCompletion)
        assert response.model_dump(exclude_unset=True) == completion
        expected_tools = [{"type": "function", "function": schema} for schema in schemas]
        assert all(request["tools"] == expected_tools for request in requests)
        assert all(request["messages"] == messages for request in requests)
        assert requests[0] == requests[2]
        assert requests[0]["stream"] is True
        assert requests[0]["stream_options"] == {"include_usage": True}
        assert "tool_choice" not in requests[0]
        assert "response_format" not in requests[0]
        assert requests[1]["stream"] is False
        assert requests[1]["tool_choice"] == "none"
        assert requests[1]["response_format"] == {"type": "json_object"}
        assert "stream_options" not in requests[1]
    finally:
        await model.close()


@pytest.mark.asyncio
async def test_sdk_stream_preserves_reasoning_tools_usage_and_request(monkeypatch) -> None:
    requests = []
    raw = [
        {
            "delta": {
                "reasoning_content": "先想",
                "content": "说明",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":'},
                    }
                ],
            },
            "finish_reason": None,
        },
        {
            "delta": {
                "reasoning_content": "再做",
                "tool_calls": [{"index": 0, "function": {"arguments": '"a"}'}}],
            },
            "finish_reason": "tool_calls",
        },
    ]
    chunks = [
        {
            "id": "r1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test",
            "choices": [{"index": 0, **c}],
        }
        for c in raw
    ]
    chunks.append(
        {
            "id": "r1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test",
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 3,
                "total_tokens": 13,
                "prompt_tokens_details": {"cached_tokens": 4},
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
        }
    )

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n",
        )

    client = AsyncOpenAI(
        api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    result = None
    snapshots = []
    schemas = [
        {
            "name": "read_file",
            "description": "read",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ]
    model = OpenAIChatModel(client, {"model": "test"}, tools=schemas)
    async for chunk in model.stream([{"role": "user", "content": "go"}]):
        snapshots.append(chunk.snapshot)
        result = chunk.snapshot.model_copy(deep=True)
    second = None
    async for chunk in model.stream(
        [
            {"role": "user", "content": "go"},
            assistant_message(result.choices[0].message, {"read_file"}),
            {
                "role": "tool",
                "content": "read",
                "tool_call_id": result.choices[0].message.tool_calls[0].id,
            },
        ]
    ):
        second = chunk.snapshot.model_copy(deep=True)
    await model.close()
    assert snapshots[0].choices[0].message.tool_calls[0].function.arguments == '{"path":'
    message = result.choices[0].message
    assert second.choices[0].message.tool_calls[0].id == message.tool_calls[0].id == "c1"
    assert requests[1]["messages"][-1]["tool_call_id"] == message.tool_calls[0].id
    assert message.content == "说明"
    assert message.model_extra["reasoning_content"] == "先想再做"
    assert message.tool_calls[0].function.arguments == '{"path":"a"}'
    assert snapshots[0].choices[0].message.tool_calls[0].id == "c1"
    assert result.choices[0].finish_reason == "tool_calls"
    assert usage_from_response(result.usage)["input_token_details"]["cache_read"] == 4
    assert "parsed" not in requests[1]["messages"][1]
    assert "index" not in requests[1]["messages"][1]["tool_calls"][0]
    assert requests[0]["messages"] == [{"role": "user", "content": "go"}]
    assert requests[0]["tools"] == [{"type": "function", "function": schemas[0]}]
    assert requests[1]["tools"] == requests[0]["tools"]
    assert "response_format" not in requests[0]
    assert client.is_closed()


def test_tool_message_uses_sdk_storage_format():
    message = {"role": "tool", "content": "result", "tool_call_id": "call"}
    assert validate_message(json.loads(json.dumps(message))) == message


def test_sdk_aggregation_preserves_incomplete_arguments_without_executing():
    events = sdk_events(
        [
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "c",
                        "type": "function",
                        "function": {"name": "write", "arguments": '{"path":'},
                    }
                ]
            }
        ],
        finish_reason="length",
    )
    final = list(events)[-1].snapshot
    assert final.choices[0].message.tool_calls[0].function.arguments == '{"path":'
    assert final.choices[0].finish_reason == "length"


@pytest.mark.parametrize(
    ("provider_usage", "expected"),
    [
        ({}, None),
        (
            {"completion_tokens": 7},
            {"input_tokens": None, "output_tokens": 7, "total_tokens": None},
        ),
        (
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        ),
    ],
)
def test_usage_preserves_unknown_and_explicit_zero(provider_usage, expected):
    """SDK 未返回的字段不补零、不推算，仍由统计层统一校验数值。"""
    from openai.types import CompletionUsage

    from app.core.workflow.execution_state.conversation_run_usage_stats import (
        ConversationRunUsageStats,
    )

    stats = ConversationRunUsageStats()
    normalized = usage_from_response(CompletionUsage.model_construct(**provider_usage))
    stats.add_usage_metadata(normalized)
    result = stats.to_dict()
    if expected is None:
        assert normalized is None
        assert result is None
        stats.add_usage_metadata({"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})
        previous = stats.to_dict()
        stats.add_usage_metadata(normalized)
        assert stats.to_dict() == previous
    else:
        assert {key: result[key] for key in expected} == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("details", ["invalid", [1, 2]])
async def test_sdk_malformed_usage_details_do_not_fail_completed_response(details):
    """实际 SDK 响应边界容忍厂商明细形状异常，完成的回答仍能交付。"""
    from app.core.workflow.execution_state.conversation_run_usage_stats import (
        ConversationRunUsageStats,
    )

    def handler(_request):
        completion = {
            "id": "r1",
            "created": 1,
            "model": "test",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "完成",
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
                "prompt_tokens_details": details,
                "completion_tokens_details": details,
            },
        }
        return httpx.Response(200, json=completion)

    client = AsyncOpenAI(
        api_key="test",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ),
    )
    model = OpenAIChatModel(client, {"model": "test"})
    try:
        result = await model.complete(
            [{"role": "user", "content": "go"}], response_format={"type": "text"}
        )
        stats = ConversationRunUsageStats()
        stats.add_usage_metadata(usage_from_response(result.usage))
        assert result.choices[0].message.content == "完成"
        assert stats.total_tokens == 12
        assert stats.cache_hit_tokens is None
        assert stats.cache_miss_tokens is None
        assert stats.reasoning_tokens is None
    finally:
        await model.close()
