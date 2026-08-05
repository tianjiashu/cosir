"""WorkspaceEventApi 单元测试。

覆盖：
- `_stream_workspace_events`：SSE 帧格式、终态 break、finally unsubscribe；
- `prepare_workspace`：404 守卫与响应结构（经 FastAPI TestClient + 依赖覆盖）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from app.models.enums.event_type import EventType
from app.models.event.workspace_event import WorkspaceEvent
from app.service.workspace_event.workspace_event_bus import WorkspaceEventBus


def _event(event_type: EventType, workspace_id: str = "ws-1") -> WorkspaceEvent:
    return WorkspaceEvent(
        event_type=event_type,
        workspace_id=workspace_id,
        workspace_path="/ws/root",
        payload={"workspace_path": "/ws/root"},
    )


def _stream(bus: WorkspaceEventBus, workspace_id: str) -> AsyncIterator[str]:
    from app.api.workspaces_api import _stream_workspace_events

    return _stream_workspace_events(bus, workspace_id)


def test_stream_emits_sse_frame_format():
    """preparing 事件应产出标准 SSE 帧 event:/data:。"""
    bus = WorkspaceEventBus()

    async def run():
        agen = _stream(bus, "ws-1")
        task = asyncio.create_task(_collect_first(agen))
        await asyncio.sleep(0)  # 让生成器启动并 subscribe
        bus.publish(_event(EventType.WORKSPACE_PREPARING, "ws-1"))
        frame = await asyncio.wait_for(task, timeout=1.0)
        return frame

    frame = asyncio.run(run())
    assert frame.startswith("event: workspace_preparing\n")
    assert 'data: {"event_id"' in frame
    assert "workspace_id" in frame
    assert frame.endswith("\n\n")


def test_stream_breaks_after_terminal_event():
    """终态事件（ready）后流应结束，不再等待后续事件。"""
    bus = WorkspaceEventBus()

    async def run():
        agen = _stream(bus, "ws-1")
        frames: list[str] = []
        task = asyncio.create_task(_collect_all(agen, frames))
        await asyncio.sleep(0)
        bus.publish(_event(EventType.WORKSPACE_PREPARING, "ws-1"))
        await asyncio.sleep(0)
        bus.publish(_event(EventType.WORKSPACE_READY, "ws-1"))
        await asyncio.wait_for(task, timeout=1.0)
        return frames

    frames = asyncio.run(run())
    assert len(frames) == 2
    assert "workspace_preparing" in frames[0]
    assert "workspace_ready" in frames[1]


def test_stream_breaks_after_degraded_terminal_event():
    """终态事件（degraded）后流应结束（对称覆盖 degraded 终态）。"""
    bus = WorkspaceEventBus()

    async def run():
        agen = _stream(bus, "ws-1")
        frames: list[str] = []
        task = asyncio.create_task(_collect_all(agen, frames))
        await asyncio.sleep(0)
        bus.publish(_event(EventType.WORKSPACE_PREPARING, "ws-1"))
        await asyncio.sleep(0)
        bus.publish(_event(EventType.WORKSPACE_DEGRADED, "ws-1"))
        await asyncio.wait_for(task, timeout=1.0)
        return frames

    frames = asyncio.run(run())
    assert len(frames) == 2
    assert "workspace_preparing" in frames[0]
    assert "workspace_degraded" in frames[1]


def test_stream_unsubscribes_in_finally():
    """流结束（正常或异常）后应退订，不残留订阅。"""
    bus = WorkspaceEventBus()

    async def run():
        agen = _stream(bus, "ws-1")
        task = asyncio.create_task(_collect_first(agen))
        await asyncio.sleep(0)
        bus.publish(_event(EventType.WORKSPACE_READY, "ws-1"))
        await asyncio.wait_for(task, timeout=1.0)
        # 终态后生成器已结束并退订；重复退订幂等
        return bus._subscribers.get("ws-1")

    remaining = asyncio.run(run())
    assert not remaining  # 订阅表已清空


async def _collect_first(agen: AsyncIterator[str]) -> str:
    """收集生成器第一帧并结束（驱动 finally 退订）。"""
    async for frame in agen:
        return frame
    raise AssertionError("stream ended without event")


async def _collect_all(agen: AsyncIterator[str], frames: list[str]) -> None:
    """收集生成器全部帧直到结束。"""
    async for frame in agen:
        frames.append(frame)


# ----------------------------------------------------------------------
# prepare 端点（直接调用端点函数，绕过 TestClient 环境兼容问题）
# ----------------------------------------------------------------------


class _FakeWorkspaceService:
    def __init__(self, root_path: str = "/ws/root") -> None:
        self._root_path = root_path
        self._missing = root_path is None

    def get_workspace(self, workspace_id: str):
        if self._missing:
            raise KeyError("workspace not found")
        return type("WS", (), {"root_path": self._root_path})()


class _FakeEventService:
    def prepare(self, workspace_id: str, root_path: str):
        from app.models.workspace_readiness import WorkspaceReadiness

        return WorkspaceReadiness(
            ready=True,
            state="ready",
            action_taken="init",
            files_changed=5,
            duration_ms=100,
            degraded_reason=None,
        )


def test_prepare_404_when_workspace_missing():
    """workspace 不存在时 prepare 抛 HTTPException 404。"""
    import pytest
    from fastapi import HTTPException

    from app.api.workspaces_api import prepare_workspace

    async def _run():
        return await prepare_workspace(
            "nonexistent",
            workspace_service=_FakeWorkspaceService(root_path=None),  # type: ignore[arg-type]
            event_service=_FakeEventService(),  # type: ignore[arg-type]
        )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(_run())
    assert excinfo.value.status_code == 404


def test_prepare_returns_readiness_response():
    """prepare 返回 WorkspacePrepareResponse，含 ready 与 action。"""
    from app.api.workspaces_api import prepare_workspace

    async def _run():
        return await prepare_workspace(
            "ws-1",
            workspace_service=_FakeWorkspaceService(),  # type: ignore[arg-type]
            event_service=_FakeEventService(),  # type: ignore[arg-type]
        )

    resp = asyncio.run(_run())
    assert resp.ready is True
    assert resp.action_taken == "init"
    assert resp.workspace_id == "ws-1"
    assert resp.files_changed == 5
    assert resp.duration_ms == 100


def test_prepare_degrades_when_event_service_none():
    """Kernel 不可用（event_service=None）时应降级返回 unavailable，不抛异常。"""
    from app.api.workspaces_api import prepare_workspace
    from app.service.workspace_event.workspace_event_bus import WorkspaceEventBus

    bus = WorkspaceEventBus()

    async def _run():
        return await prepare_workspace(
            "ws-1",
            workspace_service=_FakeWorkspaceService(),  # type: ignore[arg-type]
            event_service=None,
            event_bus=bus,
        )

    resp = asyncio.run(_run())
    assert resp.ready is False
    assert resp.state == "unavailable"
    assert resp.action_taken == "none"
    assert resp.workspace_id == "ws-1"
    assert resp.degraded_reason == "workspace event kernel unavailable"


def test_prepare_emits_degraded_event_when_event_service_none():
    """核心修复点：Kernel 不可用（event_service=None）时，端点必须主动 publish 一条
    WORKSPACE_DEGRADED 终态事件到总线——前端先连 SSE 再 POST prepare，仅靠 HTTP 响应
    不足以让 SSE 订阅者离开 preparing 状态（独立审查暴露的时序 bug 另一半）。

    注意：订阅与端点调用必须在同一事件循环内进行（先 subscribe 建立队列，再触发
    publish），否则 publish 发生在订阅之前会导致事件入队前订阅者尚未存在而丢失。
    """
    from app.api.workspaces_api import prepare_workspace
    from app.models.enums.event_type import EventType
    from app.service.workspace_event.workspace_event_bus import WorkspaceEventBus

    bus = WorkspaceEventBus()

    async def _run():
        # 先订阅建立队列，再触发端点 publish，最后消费终态事件。
        sub = bus.subscribe("ws-2")
        resp = await prepare_workspace(
            "ws-2",
            workspace_service=_FakeWorkspaceService(),  # type: ignore[arg-type]
            event_service=None,
            event_bus=bus,
        )
        event = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        return resp, event.event_type

    resp, event_type = asyncio.run(_run())
    assert resp.ready is False
    assert resp.state == "unavailable"
    assert event_type == EventType.WORKSPACE_DEGRADED


def test_prepare_timeout_degrades_when_prepare_exceeds():
    """核心降级路径 3/3：event_service.prepare 阻塞超过总超时上限时，端点必须降级返回
    state="timeout" 并 publish WORKSPACE_DEGRADED(state=timeout)。用 monkeypatch 把
    PREPARE_TIMEOUT_SECONDS 调小到 0.2s，再用一个阻塞的伪 service 触发 wait_for 超时
    （避免真实跑满 660s）。"""
    import time

    from app.api import workspaces_api
    from app.api.workspaces_api import prepare_workspace
    from app.models.enums.event_type import EventType
    from app.service.workspace_event.workspace_event_bus import WorkspaceEventBus

    class _BlockingEventService:
        def prepare(self, _workspace_id: str, _root_path: str):
            # 在线程池里阻塞 1s，超过被 monkeypatch 缩小的 0.2s 超时上限。
            time.sleep(1)
            raise AssertionError("prepare 不应在超时后返回")

    bus = WorkspaceEventBus()
    original_timeout = workspaces_api.PREPARE_TIMEOUT_SECONDS

    async def _run():
        sub = bus.subscribe("ws-3")
        resp = await prepare_workspace(
            "ws-3",
            workspace_service=_FakeWorkspaceService(),  # type: ignore[arg-type]
            event_service=_BlockingEventService(),  # type: ignore[arg-type]
            event_bus=bus,
        )
        event = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        return resp, event.event_type

    try:
        # 缩小超时，使阻塞 service 触发 wait_for 超时而不是真实等待 660s。
        workspaces_api.PREPARE_TIMEOUT_SECONDS = 0.2
        resp, event_type = asyncio.run(_run())
    finally:
        workspaces_api.PREPARE_TIMEOUT_SECONDS = original_timeout

    assert resp.ready is False
    assert resp.state == "timeout"
    assert resp.degraded_reason == "workspace event prepare exceeded timeout"
    assert event_type == EventType.WORKSPACE_DEGRADED
