"""WorkspaceEventService 单元测试。

覆盖 prepare 三分支：
- ready：发布 preparing → ready，close 被调用；
- degraded（failed）：发布 preparing → degraded；
- Kernel 不可用（unavailable）：发布 degraded，不抛异常；
- 字段映射：readiness 字段正确进入事件 payload；
- finally close：ensure_ready 抛异常时仍关闭订阅。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from app.models.enums.event_type import EventType
from app.models.event.workspace_event import WorkspaceEvent
from app.models.workspace_readiness import WorkspaceReadiness
from app.service.workspace_event.workspace_event_service import WorkspaceEventService


class _FakeLifecycle:
    """返回预设 readiness 的假 lifecycle（默认 client 可达，快检通过）。"""

    def __init__(self, readiness: WorkspaceReadiness) -> None:
        self._readiness = readiness
        self._client = _ReachableClient()

    def get_client(self):
        return self._client

    def ensure_ready(self, _workspace_path: str) -> WorkspaceReadiness:
        return self._readiness


class _RaisingLifecycle:
    """ensure_ready 抛异常的假 lifecycle（测试 finally close）。"""

    def get_client(self):
        return _ReachableClient()

    def ensure_ready(self, _workspace_path: str) -> WorkspaceReadiness:
        raise RuntimeError("unexpected")


class _PingRaisingClient:
    """ping 恒抛错的假 Kernel client（模拟进程不可达/已死）。"""

    def ping(self, timeout: float | None = None) -> None:
        raise RuntimeError("connection refused")


class _ReachableClient:
    """ping 成功的假 Kernel client（模拟进程存活）。"""

    def ping(self, timeout: float | None = None) -> None:
        return None


class _FakeLifecycleWithClient:
    """带 get_client 的假 lifecycle：client 存在但 ping 失败（快检不可达）。"""

    def __init__(self, client, readiness: WorkspaceReadiness | None = None) -> None:
        self._client = client
        self._readiness = readiness or _readiness(ready=True, action="init")

    def get_client(self):
        return self._client

    def ensure_ready(self, _workspace_path: str) -> WorkspaceReadiness:
        return self._readiness


@dataclass
class _FakeBus:
    """记录 published / closed 的假总线。"""

    published: list[WorkspaceEvent] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)

    def publish(self, event: WorkspaceEvent) -> None:
        self.published.append(event)

    def close(self, workspace_id: str) -> None:
        self.closed.append(workspace_id)


def _readiness(ready: bool, action: str = "init", state: str = "ready") -> WorkspaceReadiness:
    return WorkspaceReadiness(
        ready=ready,
        state=state,
        action_taken=action,
        files_changed=3,
        duration_ms=42,
        degraded_reason=None if ready else "kernel unavailable",
    )


def _event_types(events: list[WorkspaceEvent]) -> list[EventType]:
    return [e.event_type for e in events]


def test_prepare_ready_publishes_preparing_then_ready_and_closes():
    bus = _FakeBus()
    svc = WorkspaceEventService(_FakeLifecycle(_readiness(ready=True, action="sync")), bus)  # type: ignore[arg-type]
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
    svc = WorkspaceEventService(
        _FakeLifecycle(_readiness(ready=False, state="failed")),
        bus,  # type: ignore[arg-type]
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
    svc = WorkspaceEventService(
        _FakeLifecycle(_readiness(ready=False, state="unavailable")),
        bus,  # type: ignore[arg-type]
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


def test_prepare_health_check_unreachable_degrades_before_ensure_ready():
    """核心新增降级路径：client 存在但 ping 失败（进程不可达）时，prepare 应直接在
    进入阻塞式 ensure_ready 前发 WORKSPACE_DEGRADED(state=unreachable) 并返回，
    绝不进入最长 600s 的 index_init 阻塞。"""
    bus = _FakeBus()
    svc = WorkspaceEventService(
        _FakeLifecycleWithClient(_PingRaisingClient()),
        bus,  # type: ignore[arg-type]
    )
    result = svc.prepare("ws-1", "/ws/root")

    assert result.ready is False
    assert result.state == "unreachable"
    assert result.degraded_reason == "codegraph kernel unreachable before prepare"
    assert _event_types(bus.published) == [
        EventType.WORKSPACE_PREPARING,
        EventType.WORKSPACE_DEGRADED,
    ]
    degraded = bus.published[-1]
    assert degraded.payload["state"] == "unreachable"
    assert degraded.payload["degraded_reason"] == "codegraph kernel unreachable before prepare"
    # 快检不可达分支也必须 close，与正常/失败分支时序一致，避免订阅泄漏。
    assert bus.closed == ["ws-1"]


def test_prepare_health_check_reachable_then_ensure_ready_succeeds():
    """client 可达（ping 成功）时，prepare 应正常进入 ensure_ready 并发布 ready。"""
    bus = _FakeBus()
    svc = WorkspaceEventService(
        _FakeLifecycleWithClient(_ReachableClient()),
        bus,  # type: ignore[arg-type]
    )
    result = svc.prepare("ws-1", "/ws/root")

    assert result.ready is True
    assert _event_types(bus.published) == [
        EventType.WORKSPACE_PREPARING,
        EventType.WORKSPACE_READY,
    ]
    assert bus.closed == ["ws-1"]


def test_prepare_health_check_none_client_degrades():
    """lifecycle.get_client() 返回 None（client 未注入）时，prepare 应直接降级。"""
    bus = _FakeBus()

    class _NoClientLifecycle:
        def get_client(self):
            return None

        def ensure_ready(self, _wp: str) -> WorkspaceReadiness:
            raise AssertionError("ensure_ready 不应被调用")

    svc = WorkspaceEventService(_NoClientLifecycle(), bus)  # type: ignore[arg-type]
    result = svc.prepare("ws-1", "/ws/root")

    assert result.ready is False
    assert result.state == "unreachable"
    assert _event_types(bus.published) == [
        EventType.WORKSPACE_PREPARING,
        EventType.WORKSPACE_DEGRADED,
    ]
    assert bus.closed == ["ws-1"]


def test_prepare_closes_bus_in_finally_even_on_error():
    """ensure_ready 抛异常时，prepare 也应 finally 关闭订阅（不泄漏）。"""
    bus = _FakeBus()
    svc = WorkspaceEventService(_RaisingLifecycle(), bus)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        svc.prepare("ws-1", "/ws/root")
    assert bus.closed == ["ws-1"]


def test_prepare_maps_readiness_fields_into_payload():
    """就绪事件 payload 必须包含 action_taken/files_changed/duration_ms。"""
    bus = _FakeBus()
    svc = WorkspaceEventService(_FakeLifecycle(_readiness(ready=True, action="init")), bus)  # type: ignore[arg-type]
    svc.prepare("ws-9", "/ws/root")
    ready = bus.published[-1]
    assert ready.payload["action_taken"] == "init"
    assert ready.payload["files_changed"] == 3
    assert ready.payload["duration_ms"] == 42
    assert ready.payload["workspace_path"] == "/ws/root"


def test_prepare_with_real_bus_delivers_terminal_event_to_subscriber():
    """集成验证：用真实 WorkspaceEventBus，订阅者应收到 [PREPARING, READY]。

    覆盖独立审查暴露的致命时序：close 必须在 emit 终态之后执行，否则
    ready/degraded 被 close 后 noop 吞掉，SSE 流只收到 preparing。
    """

    from app.models.enums.event_type import EventType
    from app.service.workspace_event.workspace_event_bus import WorkspaceEventBus

    bus = WorkspaceEventBus()
    svc = WorkspaceEventService(_FakeLifecycle(_readiness(ready=True, action="sync")), bus)  # type: ignore[arg-type]

    async def run():
        sub = bus.subscribe("ws-1")
        # 后台消费，模拟 SSE 流
        svc.prepare("ws-1", "/ws/root")
        received: list[EventType] = []
        # 消费两个事件；第三轮应因 close 哨兵结束。加超时兜底，避免实现回归时永久挂起。
        for _ in range(2):
            received.append((await asyncio.wait_for(sub.__anext__(), timeout=1.0)).event_type)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        return received

    received = asyncio.run(run())
    assert received == [EventType.WORKSPACE_PREPARING, EventType.WORKSPACE_READY]
