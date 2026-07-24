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
from app.storage.crud.runtime_event_crud import RuntimeEventCrud


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
            payload=RunStartedPayload(status="running", agent_id="developer"),
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
    assert data["payload"] == {"status": "running", "agent_id": "developer"}


def test_delete_task_cascades_turns_and_returns_404() -> None:
    """删除任务应级联清理其轮次，且删除后查询返回 404。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当删除级联或 404 守卫不符合预期时。

    副作用:
        在测试用存储中创建并清理一个工作区（净零残留）。
    """

    app = create_app(runtime=_FakeRuntime())

    with TestClient(app) as client:
        ws = client.post("/workspaces", json={"name": "del-ws", "root_path": "/tmp/del-ws"})  # noqa: S108
        assert ws.status_code == 200
        workspace_id = ws.json()["workspace_id"]
        try:
            task = client.post(
                f"/workspaces/{workspace_id}/tasks",
                json={"text": "del me", "workspace_id": workspace_id},
            )
            assert task.status_code == 200
            task_id = task.json()["task_id"]

            # 删除任务：应级联其下首个 turn
            deleted = client.delete(f"/tasks/{task_id}")
            assert deleted.status_code == 200
            assert deleted.json()["deleted"] is True

            # 任务已不可查（404 守卫），其下轮次级联清空（返回空列表）
            assert client.get(f"/tasks/{task_id}").status_code == 404
            turns_after = client.get(f"/tasks/{task_id}/turns")
            assert turns_after.status_code == 200
            assert turns_after.json() == []

            # 不存在的任务删除返回 404
            assert client.delete("/tasks/ghost-task").status_code == 404
        finally:
            client.delete(f"/workspaces/{workspace_id}")


def test_delete_workspace_cascades_runtime_events() -> None:
    """删除工作区应级联清理其下任务的运行时事件。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当级联清理 runtime_events 不符合预期时。

    副作用:
        在测试用存储中创建并清理一个工作区（净零残留）。
    """

    app = create_app(runtime=_FakeRuntime())

    with TestClient(app) as client:
        ws = client.post("/workspaces", json={"name": "ws-events", "root_path": "/tmp/ws-events"})  # noqa: S108
        assert ws.status_code == 200
        workspace_id = ws.json()["workspace_id"]
        try:
            task = client.post(
                f"/workspaces/{workspace_id}/tasks",
                json={"text": "del me", "workspace_id": workspace_id},
            )
            assert task.status_code == 200
            task_id = task.json()["task_id"]

            # 为该任务创建轮次，模拟一次真实执行单元
            turn = client.post(f"/tasks/{task_id}/turns", json={"input_text": "run something"})
            assert turn.status_code == 200
            turn_id = turn.json()["turn_id"]

            # 手动落库一条运行时事件（真实事件由 SSE 流写入，测试里直接构造）
            RuntimeEventCrud().save_event(
                {
                    "event_id": "ev-cascade-1",
                    "event_type": "run_started",
                    "task_id": task_id,
                    "turn_id": turn_id,
                    "sequence": 0,
                    "payload": {"status": "running", "agent_id": "developer"},
                    "created_at": "2026-07-24T00:00:00+00:00",
                }
            )
            assert len(RuntimeEventCrud().list_by_turn(turn_id)) == 1

            # 删除工作区：应级联清理其下轮次与运行时事件
            deleted = client.delete(f"/workspaces/{workspace_id}")
            assert deleted.status_code == 200

            # 运行时事件随工作区级联删除而被清空
            assert RuntimeEventCrud().list_by_turn(turn_id) == []
            assert RuntimeEventCrud().list_by_task(task_id) == []
        finally:
            client.delete(f"/workspaces/{workspace_id}")
