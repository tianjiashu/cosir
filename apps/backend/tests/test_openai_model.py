"""真实 SDK + 本机 HTTP mock 覆盖消息边界与流式聚合。"""

import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI

from app.core.llm_provider.cancellable_stream import ModelRequestCancelled, cancellable_stream
from app.core.llm_provider.message_codec import assistant_message, validate_message
from app.core.llm_provider.openai_model import OpenAIChatModel, usage_from_response
from tests.sdk_fixtures import sdk_events


@pytest.mark.asyncio
async def test_sdk_stream_preserves_reasoning_tools_usage_and_request():
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
    model = OpenAIChatModel(client, {"model": "test"})
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
    async for chunk in model.stream(
        [{"role": "user", "content": "go"}],
        tools=schemas,
    ):
        snapshots.append(chunk.snapshot)
        result = chunk.snapshot.model_copy(deep=True)
    second = None
    async for chunk in model.stream(
        [
            {"role": "user", "content": "go"},
            assistant_message(result.choices[0].message),
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
    assert second.choices[0].message.tool_calls[0].id != message.tool_calls[0].id
    assert requests[1]["messages"][-1]["tool_call_id"] == message.tool_calls[0].id
    assert message.content == "说明"
    assert message.model_extra["reasoning_content"] == "先想再做"
    assert message.tool_calls[0].function.arguments == '{"path":"a"}'
    assert message.tool_calls[0].id.startswith("call_")
    assert result.choices[0].finish_reason == "tool_calls"
    assert usage_from_response(result.usage)["input_token_details"]["cache_read"] == 4
    assert "parsed" not in requests[1]["messages"][1]
    assert "index" not in requests[1]["messages"][1]["tool_calls"][0]
    assert requests[0]["messages"] == [{"role": "user", "content": "go"}]
    assert requests[0]["tools"] == [{"type": "function", "function": schemas[0]}]
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


@pytest.mark.asyncio
async def test_cancel_while_waiting_first_token_closes_network_generator():
    closed = asyncio.Event()
    cancelled = False

    async def source():
        try:
            await asyncio.Event().wait()
            yield "unreachable"
        finally:
            closed.set()

    async def consume():
        async for _ in cancellable_stream(source(), lambda: cancelled):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.02)
    cancelled = True
    with pytest.raises(ModelRequestCancelled):
        await asyncio.wait_for(task, timeout=1)
    assert closed.is_set()


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

    from app.core.workflow.conversation_run_usage_stats import ConversationRunUsageStats

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
    from app.core.workflow.conversation_run_usage_stats import ConversationRunUsageStats

    def handler(_request):
        chunk = {
            "id": "r1",
            "created": 1,
            "model": "test",
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "delta": {
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
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
        )

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
