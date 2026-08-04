"""WorkspaceIndexService 单元测试。

覆盖 prepare 三分支：
- ready：发布 preparing → ready，close 被调用；
- degraded（failed）：发布 preparing → degraded；
- Kernel 不可用（unavailable）：发布 degraded，不抛异常；
- 字段映射：readiness 字段正确进入事件 payload；
- finally close：ensure_ready 抛异常时仍关闭订阅。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.models.enums.event_type import EventType
from app.models.workspace_index_event import WorkspaceIndexEvent
from app.models.workspace_index_readiness import WorkspaceIndexReadiness
from app.service.codegraph.workspace_index_service import WorkspaceIndexService


class _FakeLifecycle:
    """返回预设 readiness 的假 lifecycle。"""

    def __init__(self, readiness: WorkspaceIndexReadiness) -> None:
        self._readiness = readiness

    def ensure_ready(self, _workspace_path: str) -> WorkspaceIndexReadiness:
        return self._readiness


class _RaisingLifecycle:
    """ensure_ready 抛异常的假 lifecycle（测试 finally close）。"""

    def ensure_ready(self, _workspace_path: str) -> WorkspaceIndexReadiness:
        raise RuntimeError("unexpected")


@dataclass
class _FakeBus:
    """记录 published / closed 的假总线。"""

    published: list[WorkspaceIndexEvent] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)

    def publish(self, event: WorkspaceIndexEvent) -> None:
        self.published.append(event)

    def close(self, workspace_id: str) -> None:
        self.closed.append(workspace_id)


def _readiness(ready: bool, action: str = "init", state: str = "ready") -> WorkspaceIndexReadiness:
    return WorkspaceIndexReadiness(
        ready=ready,
        state=state,
        action_taken=action,
        files_changed=3,
        duration_ms=42,
        degraded_reason=None if ready else "kernel unavailable",
    )


def _event_types(events: list[WorkspaceIndexEvent]) -> list[EventType]:
    return [e.event_type for e in events]


def test_prepare_ready_publishes_preparing_then_ready_and_closes():
    bus = _FakeBus()
    svc = WorkspaceIndexService(_FakeLifecycle(_readiness(ready=True, action="sync")), bus)  # type: ignore[arg-type]
    result = svc.prepare("ws-1", "/ws/root")

    assert result.ready is True
    assert _event_types(bus.published) == [EventType.WORKSPACE_PREPARING, EventType.WORKSPACE_READY]
    ready = bus.published[-1]
    assert ready.workspace_id == "ws-1"
    assert ready.workspace_path == "/ws/root"
    assert ready.payload["action_taken"] == "sync"
    assert ready.payload["files_changed"] == 3
    assert ready.payload["duration_ms"] == 42
    assert bus.closed == ["ws-1"]


def test_prepare_degraded_publishes_preparing_then_degraded_and_closes():
    bus = _FakeBus()
    svc = WorkspaceIndexService(
        _FakeLifecycle(_readiness(ready=False, state="failed")), bus  # type: ignore[arg-type]
    )
    result = svc.prepare("ws-1", "/ws/root")

    assert result.ready is False
    assert _event_types(bus.published) == [
        EventType.WORKSPACE_PREPARING,
        EventType.WORKSPACE_DEGRADED,
    ]
    degraded = bus.published[-1]
    assert degraded.payload["state"] == "failed"
    assert degraded.payload["degraded_reason"] == "kernel unavailable"
    assert bus.closed == ["ws-1"]


def test_prepare_kernel_unavailable_degraded_not_raise():
    bus = _FakeBus()
    svc = WorkspaceIndexService(
        _FakeLifecycle(_readiness(ready=False, state="unavailable")), bus  # type: ignore[arg-type]
    )
    # 不应抛异常
    result = svc.prepare("ws-1", "/ws/root")

    assert result.ready is False
    assert result.state == "unavailable"
    assert _event_types(bus.published) == [
        EventType.WORKSPACE_PREPARING,
        EventType.WORKSPACE_DEGRADED,
    ]
    assert bus.closed == ["ws-1"]


def test_prepare_closes_bus_in_finally_even_on_error():
    """ensure_ready 抛异常时，prepare 也应 finally 关闭订阅（不泄漏）。"""
    bus = _FakeBus()
    svc = WorkspaceIndexService(_RaisingLifecycle(), bus)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        svc.prepare("ws-1", "/ws/root")
    assert bus.closed == ["ws-1"]


def test_prepare_maps_readiness_fields_into_payload():
    """就绪事件 payload 必须包含 action_taken/files_changed/duration_ms。"""
    bus = _FakeBus()
    svc = WorkspaceIndexService(_FakeLifecycle(_readiness(ready=True, action="init")), bus)  # type: ignore[arg-type]
    svc.prepare("ws-9", "/ws/root")
    ready = bus.published[-1]
    assert ready.payload["action_taken"] == "init"
    assert ready.payload["files_changed"] == 3
    assert ready.payload["duration_ms"] == 42
    assert ready.payload["workspace_path"] == "/ws/root"


def test_prepare_with_real_bus_delivers_terminal_event_to_subscriber():
    """集成验证：用真实 WorkspaceIndexBus，订阅者应收到 [PREPARING, READY]。

    覆盖独立审查暴露的致命时序：close 必须在 emit 终态之后执行，否则
    ready/degraded 被 close 后 noop 吞掉，SSE 流只收到 preparing。
    """
    import asyncio

    from app.models.enums.event_type import EventType
    from app.service.codegraph.workspace_index_bus import WorkspaceIndexBus

    bus = WorkspaceIndexBus()
    svc = WorkspaceIndexService(_FakeLifecycle(_readiness(ready=True, action="sync")), bus)  # type: ignore[arg-type]

    async def run():
        sub = bus.subscribe("ws-1")
        # 后台消费，模拟 SSE 流
        svc.prepare("ws-1", "/ws/root")
        received: list[EventType] = []
        # 消费两个事件；第三轮应因 close 哨兵结束
        for _ in range(2):
            received.append((await sub.__anext__()).event_type)
        with pytest.raises(StopAsyncIteration):
            await sub.__anext__()
        return received

    received = asyncio.run(run())
    assert received == [EventType.WORKSPACE_PREPARING, EventType.WORKSPACE_READY]
