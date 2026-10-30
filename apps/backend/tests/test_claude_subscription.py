"""订阅 provider 的真实传输边界：流式投影、原生回传及失败收敛。"""

import asyncio
import json
from contextlib import aclosing

import httpx
import pytest
from openai import AsyncOpenAI

from app.core.llm_provider.claude.errors import ClaudeAPIError, ClaudeProtocolError
from app.core.llm_provider.claude.model import ClaudeChatModel
from app.core.llm_provider.message_codec import assistant_message, validate_message
from app.core.llm_provider.openai_model import OpenAIChatModel
from tests.claude_fixtures import sonnet_info


async def token():
    return "test-subscription-token"


def frames(*, tool=False, thinking=False, finish=True):
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [],
                "usage": {
                    "input_tokens": 7,
                    "cache_read_input_tokens": 11,
                    "cache_creation_input_tokens": 3,
                    "output_tokens": 0,
                },
            },
        }
    ]
    index = 0
    if thinking:
        events += [
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "thinking_delta", "thinking": "计划"},
            },
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "signature_delta", "signature": "opaque-signature"},
            },
            {"type": "content_block_stop", "index": index},
        ]
        index += 1
    events += [
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": "hello "},
        },
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": "world"},
        },
        {"type": "content_block_stop", "index": index},
    ]
    if tool:
        index += 1
        events += [
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "local_sum",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "input_json_delta", "partial_json": '{"a":17,'},
            },
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "input_json_delta", "partial_json": '"b":25}'},
            },
            {"type": "content_block_stop", "index": index},
        ]
    if finish:
        events += [
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use" if tool else "end_turn"},
                "usage": {"output_tokens": 8},
            },
            {"type": "message_stop"},
        ]
    return "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events)


@pytest.mark.asyncio
async def test_stream_and_native_tool_round_trip_survive_serialization():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer test-subscription-token"
        assert "x-api-key" not in request.headers
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=frames(tool=len(requests) == 1, thinking=True),
        )

    model = ClaudeChatModel(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        {"model": "claude-sonnet-4-6", "max_tokens": 2048},
        tools=[{"name": "local_sum", "parameters": {"type": "object", "properties": {}}}],
        token_provider=token,
        model_info=sonnet_info(),
    )
    try:
        events = [
            e
            async for e in model.stream(
                [{"role": "system", "content": "Own agent"}, {"role": "user", "content": "go"}]
            )
        ]
        assert [
            e.chunk.choices[0].delta.content
            for e in events
            if e.chunk.choices and e.chunk.choices[0].delta.content
        ] == ["hello ", "world"]
        final = events[-1].snapshot
        assert final.choices[0].finish_reason == "tool_calls"
        assert final.usage.prompt_tokens == 21
        assert final.usage.completion_tokens == 8
        assert final.usage.prompt_tokens_details.cached_tokens == 11
        assert events[0].snapshot.choices[0].message.content != "hello world"
        saved = assistant_message(final.choices[0].message, {"local_sum"})
        restored = validate_message(json.loads(json.dumps(saved)))
        second = [
            e
            async for e in model.stream(
                [
                    {"role": "user", "content": "go"},
                    restored,
                    {"role": "tool", "tool_call_id": "toolu_1", "content": "42"},
                ]
            )
        ]
        assert second[-1].snapshot.choices[0].finish_reason == "stop"
        native = requests[1]["messages"][1]["content"]
        assert native[0] == {
            "type": "thinking",
            "thinking": "计划",
            "signature": "opaque-signature",
        }
        assert native[-1]["input"] == {"a": 17, "b": 25}
        assert requests[1]["messages"][-1] == {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "42"}],
        }
        assert requests[0]["system"][1]["text"] == "Own agent"
    finally:
        await model.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,payload",
    [
        (429, '{"error":{"type":"rate_limit_error","message":"limited"}}'),
        (200, frames(finish=False)),
        (
            200,
            'event: error\ndata: {"type":"error",'
            '"error":{"type":"overloaded_error","message":"busy"}}\n\n',
        ),
    ],
)
async def test_errors_and_truncated_stream_are_not_success_or_retried(status, payload):
    count = 0

    def handler(_):
        nonlocal count
        count += 1
        return httpx.Response(
            status, headers={"content-type": "text/event-stream"}, content=payload
        )

    model = ClaudeChatModel(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        {"model": "test"},
        token_provider=token,
        model_info=sonnet_info(),
    )
    try:
        with pytest.raises((ClaudeAPIError, ClaudeProtocolError)):
            _ = [e async for e in model.stream([{"role": "user", "content": "go"}])]
        assert count == 1
    finally:
        await model.close()


@pytest.mark.asyncio
async def test_closing_or_cancelling_stream_closes_http_body():
    class Body(httpx.AsyncByteStream):
        def __init__(self):
            self.closed = False
            self.waiting = asyncio.Event()

        async def __aiter__(self):
            yield frames(finish=False).encode()
            self.waiting.set()
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    body = Body()
    model = ClaudeChatModel(
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=body
                )
            )
        ),
        {"model": "test"},
        token_provider=token,
        model_info=sonnet_info(),
    )

    async def consume():
        async with aclosing(model.stream([{"role": "user", "content": "go"}])) as stream:
            async for _ in stream:
                pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(body.waiting.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert body.closed
    await model.close()


@pytest.mark.asyncio
async def test_switch_to_openai_strips_native_blocks_from_both_wire_paths():
    native = {"type": "thinking", "thinking": "plan", "signature": "private-signature"}
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "ready", "claude_content": [native]},
        {"role": "user", "content": "next"},
    ]
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        response = {
            "id": "response",
            "model": "test",
            "created": 0,
            "object": "chat.completion.chunk" if body["stream"] else "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "delta" if body["stream"] else "message": {
                        "role": "assistant",
                        "content": "{}",
                    },
                }
            ],
        }
        if not body["stream"]:
            return httpx.Response(200, json=response)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="data: " + json.dumps(response) + "\n\ndata: [DONE]\n\n",
        )

    model = OpenAIChatModel(
        AsyncOpenAI(
            api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        ),
        {"model": "test"},
    )
    try:
        assert [event async for event in model.stream(messages)]
        await model.complete(messages, response_format={"type": "json_object"})
        assert len(requests) == 2
        for body in requests:
            assert body["messages"][1] == {"role": "assistant", "content": "ready"}
            assert "private-signature" not in json.dumps(body)
        assert messages[1]["claude_content"] == [native]
    finally:
        await model.close()
