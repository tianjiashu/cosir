"""FastAPI app wiring integration tests."""

import json
from collections.abc import AsyncIterator
from typing import cast

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.turns_api import _sse_turn_events
from app.core.agents.agent_profile import default_developer_agent
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.runtime.runner import AgentRuntime
from app.models.enums.event_type import EventType
from app.models.payload import RunStartedPayload
from app.models.runtime_event import RuntimeEvent


class _FakeRuntime:
    """测试用运行时替身。"""

    def __init__(self) -> None:
        """初始化替身运行时。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            构造只含默认 agent 的 registry。
        """

        registry = AgentProfileRegistry()
        registry.register(default_developer_agent())
        self.agent_registry = registry

    def backend_health(self) -> dict[str, object]:
        """返回健康检查响应替身。

        参数:
            无。

        返回:
            与 ``HealthResponse`` 对齐的健康状态字典。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "status": "ok",
            "model_provider": "echo",
            "model_base_url": "https://api.deepseek.com",
            "model_name": "deepseek-v4-flash",
            "model_thinking_mode": "disabled",
            "model_api_key_env": "DEEPSEEK_API_KEY",
            "has_model_api_key": False,
        }


class _FakeStreamRuntime:
    """测试用 SSE 运行时替身。"""

    async def run_turn(
        self,
        turn_id: str,
        turn: object | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        """产出一个真实运行时事件。

        参数:
            turn_id: 测试轮次标识。
            turn: 可选轮次对象，本替身不读取。

        返回:
            异步事件迭代器。

        异常:
            无。

        副作用:
            无。
        """

        yield RuntimeEvent(
            event_type=EventType.RUN_STARTED,
            task_id="task-1",
            turn_id=turn_id,
            payload=RunStartedPayload(status="running", agent_id={}),
        )


def test_create_app_lifespan_serves_health_agents_and_openapi() -> None:
    """FastAPI app 默认 lifespan 能启动并服务关键 API。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当 app lifespan、关键 API 或 OpenAPI schema 不符合预期时。

    副作用:
        启动一次 FastAPI TestClient，并通过 runtime override 避免写真实运行时存储。
    """

    app = create_app(runtime=_FakeRuntime())

    with TestClient(app) as client:
        health = client.get("/health")
        agents = client.get("/agents")
        openapi = client.get("/openapi.json")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert agents.status_code == 200
    assert agents.json()["default_agent_id"] == "developer"
    assert openapi.status_code == 200
    assert "/agents" in openapi.json()["paths"]


@pytest.mark.asyncio
async def test_sse_turn_events_serializes_payload_entity_for_client() -> None:
    """SSE 边界把 Payload 实体序列化为客户端可消费 JSON。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当 SSE 帧或 payload JSON 不符合契约时。

    副作用:
        运行一次测试用异步事件迭代器。
    """

    frames = [
        frame
        async for frame in _sse_turn_events(
            cast(AgentRuntime, _FakeStreamRuntime()),
            "turn-1",
        )
    ]

    assert len(frames) == 1
    assert frames[0].startswith("event: run_started\n")
    data = json.loads(frames[0].split("data: ", maxsplit=1)[1])
    assert data["event_type"] == "run_started"
    assert data["turn_id"] == "turn-1"
    assert data["payload"] == {"status": "running", "agent": {}}
