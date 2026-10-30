"""以目录返回能力决定请求参数，不按模型名称猜测版本。"""

import asyncio
import json

import httpx
import pytest

from app.core.llm_provider.claude.catalog import ClaudeModelInfo, fetch_models
from app.core.llm_provider.claude.errors import ClaudeAPIError, ClaudeProtocolError
from app.core.llm_provider.claude.model import ClaudeChatModel
from tests.claude_fixtures import model_data, sonnet_info


async def token():
    return "test-token"


@pytest.mark.asyncio
async def test_catalog_paginates_and_exposes_supported_choice_fields():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer test-token"
        assert request.headers["anthropic-beta"] == "oauth-2025-04-20"
        first = len(requests) == 1
        return httpx.Response(
            200,
            json={
                "data": [model_data("first" if first else "second")],
                "has_more": first,
                "last_id": "first" if first else "second",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        models = await fetch_models(client, "test-token")
    assert requests[1].url.params["after_id"] == "first"
    assert [m.id for m in models] == ["first", "second"]
    assert models[0].choice() == {
        "id": "first",
        "display_name": "first",
        "context_window_k": 1000,
        "supports_thinking": True,
        "supports_reasoning_effort": True,
        "supports_image": True,
    }


@pytest.mark.asyncio
async def test_required_adaptive_thinking_survives_probe_and_json_branch():
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [model_data("new-model", enabled=False, disabled=False)],
                    "has_more": False,
                    "last_id": "new-model",
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "response",
                "model": "new-model",
                "role": "assistant",
                "content": [{"type": "text", "text": "{}"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    model = ClaudeChatModel(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        {"model": "new-model", "max_tokens": 1, "temperature": 0.4},
        thinking_enabled=False,
        reasoning_effort="max",
        token_provider=token,
    )
    try:
        await asyncio.gather(
            *[
                model.complete(
                    [{"role": "user", "content": "go"}], response_format={"type": "json_object"}
                )
                for _ in range(2)
            ]
        )
        assert len([r for r in requests if r.method == "GET"]) == 1
        for request in requests[1:]:
            body = json.loads(request.content)
            assert body["thinking"] == {"type": "adaptive"}
            assert body["output_config"] == {"effort": "high"}
            assert body["max_tokens"] == 1
            assert "temperature" not in body
    finally:
        await model.close()


@pytest.mark.parametrize(
    "info,maximum,wanted,expected",
    [
        (sonnet_info(), 2048, True, {"type": "enabled", "budget_tokens": 1024}),
        (sonnet_info(), 1, True, None),
        (
            ClaudeModelInfo.model_validate(model_data("adaptive", enabled=False)),
            2048,
            True,
            {"type": "adaptive"},
        ),
        (
            ClaudeModelInfo.model_validate(
                model_data("plain", enabled=False, adaptive=False, effort=False)
            ),
            2048,
            True,
            None,
        ),
    ],
)
@pytest.mark.asyncio
async def test_thinking_uses_capabilities_and_legacy_budget(info, maximum, wanted, expected):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'data: {"type":"message_start","message":{"id":"test","model":"test",'
                '"content":[],"usage":{"input_tokens":1,"output_tokens":0}}}\n\n'
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
                '"usage":{"output_tokens":0}}\n\n'
                'data: {"type":"message_stop"}\n\n'
            ),
        )

    model = ClaudeChatModel(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        {"model": info.id, "max_tokens": maximum},
        thinking_enabled=wanted,
        reasoning_effort="low",
        token_provider=token,
        model_info=info,
    )
    try:
        assert [event async for event in model.stream([{"role": "user", "content": "go"}])]
        assert bodies[0].get("thinking") == expected
    finally:
        await model.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 429])
async def test_invalid_catalog_does_not_loop_or_expose_credentials(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            json={
                "data": [],
                "has_more": True,
                "last_id": None,
                "error": {"message": "test-token"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises((ClaudeProtocolError, ClaudeAPIError)) as captured:
            await fetch_models(client, "test-token")
    assert len(requests) == 1
    assert "test-token" not in str(captured.value)
