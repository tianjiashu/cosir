"""WorkspaceIndexApi 单元测试。

覆盖：
- `_stream_workspace_index_events`：SSE 帧格式、终态 break、finally unsubscribe；
- `prepare_workspace_index`：404 守卫与响应结构（经 FastAPI TestClient + 依赖覆盖）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from app.models.enums.event_type import EventType
from app.models.workspace_index_event import WorkspaceIndexEvent
from app.service.codegraph.workspace_index_bus import WorkspaceIndexBus


def _event(event_type: EventType, workspace_id: str = "ws-1") -> WorkspaceIndexEvent:
    return WorkspaceIndexEvent(
        event_type=event_type,
        workspace_id=workspace_id,
        workspace_path="/ws/root",
        payload={"workspace_path": "/ws/root"},
    )


def _stream(bus: WorkspaceIndexBus, workspace_id: str) -> AsyncIterator[str]:
    from app.api.workspace_index_api import _stream_workspace_index_events

    return _stream_workspace_index_events(bus, workspace_id)


def test_stream_emits_sse_frame_format():
    """preparing 事件应产出标准 SSE 帧 event:/data:。"""
    bus = WorkspaceIndexBus()

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
    bus = WorkspaceIndexBus()

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
    bus = WorkspaceIndexBus()

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
    bus = WorkspaceIndexBus()

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


class _FakeIndexService:
    def prepare(self, workspace_id: str, root_path: str):
        from app.models.workspace_index_readiness import WorkspaceIndexReadiness

        return WorkspaceIndexReadiness(
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

    from app.api.workspace_index_api import prepare_workspace_index

    async def _run():
        return await prepare_workspace_index(
            "nonexistent",
            workspace_service=_FakeWorkspaceService(root_path=None),  # type: ignore[arg-type]
            index_service=_FakeIndexService(),  # type: ignore[arg-type]
        )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(_run())
    assert excinfo.value.status_code == 404


def test_prepare_returns_readiness_response():
    """prepare 返回 IndexPrepareResponse，含 ready 与 action。"""
    from app.api.workspace_index_api import prepare_workspace_index

    async def _run():
        return await prepare_workspace_index(
            "ws-1",
            workspace_service=_FakeWorkspaceService(),  # type: ignore[arg-type]
            index_service=_FakeIndexService(),  # type: ignore[arg-type]
        )

    resp = asyncio.run(_run())
    assert resp.ready is True
    assert resp.action_taken == "init"
    assert resp.workspace_id == "ws-1"
    assert resp.files_changed == 5
    assert resp.duration_ms == 100


def test_prepare_degrades_when_index_service_none():
    """Kernel 不可用（index_service=None）时应降级返回 unavailable，不抛异常。"""
    from app.api.workspace_index_api import prepare_workspace_index

    async def _run():
        return await prepare_workspace_index(
            "ws-1",
            workspace_service=_FakeWorkspaceService(),  # type: ignore[arg-type]
            index_service=None,
        )

    resp = asyncio.run(_run())
    assert resp.ready is False
    assert resp.state == "unavailable"
    assert resp.action_taken == "none"
    assert resp.workspace_id == "ws-1"
    assert resp.degraded_reason == "codegraph kernel unavailable"
