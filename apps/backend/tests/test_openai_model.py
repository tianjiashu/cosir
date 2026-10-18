"""真实 SDK + 本机 HTTP mock 覆盖消息边界与流式聚合。"""

import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI

from app.core.llm_provider.cancellable_stream import ModelRequestCancelled, cancellable_stream
from app.core.llm_provider.messages import AIMessageChunk, HumanMessage, ToolMessage
from app.core.llm_provider.openai_model import OpenAIChatModel, to_provider_message


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
        [HumanMessage(content="go", additional_kwargs={"user_input": {"secret": "local"}})],
        tools=schemas,
    ):
        result = chunk if result is None else result + chunk
    second = None
    async for chunk in model.stream(
        [
            HumanMessage(content="go"),
            result,
            ToolMessage(content="read", tool_call_id=result.tool_calls[0]["id"]),
        ]
    ):
        second = chunk if second is None else second + chunk
    await model.close()
    assert second.tool_calls[0]["id"] != result.tool_calls[0]["id"]
    assert requests[1]["messages"][-1]["tool_call_id"] == result.tool_calls[0]["id"]
    assert result.content == "说明"
    assert result.additional_kwargs["reasoning_content"] == "先想再做"
    assert result.tool_calls == [
        {
            "id": result.tool_calls[0]["id"],
            "name": "read_file",
            "args": {"path": "a"},
            "type": "tool_call",
        }
    ]
    assert result.tool_calls[0]["id"].startswith("call_")
    assert result.response_metadata["finish_reason"] == "tool_calls"
    assert result.usage_metadata["input_token_details"]["cache_read"] == 4
    assert requests[0]["messages"] == [{"role": "user", "content": "go"}]
    assert requests[0]["tools"] == [{"type": "function", "function": schemas[0]}]
    assert client.is_closed()


def test_tool_message_has_only_provider_fields():
    message = ToolMessage(content="result", tool_call_id="call", artifact={"local": True})
    assert to_provider_message(message) == {
        "role": "tool",
        "content": "result",
        "tool_call_id": "call",
    }


def test_incomplete_json_never_becomes_executable():
    chunk = AIMessageChunk(
        tool_call_chunks=[{"index": 0, "id": "c", "name": "write", "args": '{"path":'}]
    )
    chunk.parse_calls()
    assert not chunk.tool_calls
    assert chunk.invalid_tool_calls[0]["id"] == "c"


@pytest.mark.asyncio
async def test_cancel_while_waiting_first_token_closes_network_generator():
    closed = asyncio.Event()
    cancelled = False

    async def source():
        try:
            await asyncio.Event().wait()
            yield AIMessageChunk(content="unreachable")
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
