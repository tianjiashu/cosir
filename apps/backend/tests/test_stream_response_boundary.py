"""真实 SDK 流保留服务端响应，身份准入不属于网络适配层。"""

import json

import httpx
import pytest
from openai import AsyncOpenAI

from app.core.llm_provider.message_codec import assistant_message
from app.core.llm_provider.openai_model import OpenAIChatModel


class ResponseBody(httpx.AsyncByteStream):
    """记录网络流关闭；测试不得用关闭 client 代替关闭单次响应。"""

    def __init__(self, choices):
        self.choices = choices
        self.closed = False

    async def __aiter__(self):
        for choices in self.choices:
            chunk = {
                "id": "response",
                "created": 1,
                "model": "test",
                "object": "chat.completion.chunk",
                "choices": choices,
            }
            yield ("data: " + json.dumps(chunk) + "\n\n").encode()
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True


def _model(body):
    return OpenAIChatModel(
        AsyncOpenAI(
            api_key="test",
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        200, headers={"content-type": "text/event-stream"}, stream=body
                    )
                )
            ),
        ),
        {"model": "test"},
    )


@pytest.mark.asyncio
async def test_empty_first_frame_and_multiple_choices_reach_consumer():
    """首帧不丢弃；适配层保留全部候选，不声明 workflow 支持多候选。"""
    body = ResponseBody(
        [
            [],
            [
                {
                    "index": index,
                    "delta": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
                for index, text in enumerate(["first", "second"])
            ],
        ]
    )
    model = _model(body)
    try:
        events = [event async for event in model.stream([])]
        assert body.closed
        assert len(events) == 2
        assert events[0].chunk.choices == events[0].snapshot.choices == []
        assert [choice.index for choice in events[1].chunk.choices] == [0, 1]
        assert [choice.message.content for choice in events[1].snapshot.choices] == [
            "first",
            "second",
        ]
    finally:
        await model.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ids, expected", [(["c", "1"], "c1"), (["c1", "c1"], "c1c1"), ([None, None], "")]
)
async def test_tool_id_deltas_are_not_rewritten_or_suppressed(monkeypatch, ids, expected):
    """ID 完全遵循 SDK 增量拼接；缺失身份的调用在投影时被丢弃。"""
    frames = []
    for index, call_id in enumerate(ids):
        call = {"index": 0, "function": {"arguments": "{" if index == 0 else "}"}}
        if index == 0:
            call.update(type="function")
            call["function"]["name"] = "read_file"
        if call_id is not None:
            call["id"] = call_id
        frames.append(
            [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "tool_calls": [call]},
                    "finish_reason": None if index == 0 else "tool_calls",
                }
            ]
        )
    body = ResponseBody(frames)
    model = _model(body)
    try:
        events = [event async for event in model.stream([])]
        assert body.closed
        assert [event.chunk.choices[0].delta.tool_calls[0].id for event in events] == ids
        message = assistant_message(events[-1].snapshot.choices[0].message, {"read_file"})
        if expected:
            assert message["tool_calls"][0]["id"] == expected
            assert message["tool_calls"][0]["function"]["arguments"] == "{}"
        else:
            assert "tool_calls" not in message
    finally:
        await model.close()
