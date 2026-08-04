"""WorkspaceIndexBus 单元测试。

覆盖：
- subscribe 按 workspace_id 建订阅队列；
- publish 只向已注册订阅分发（未订阅/已退订不投递，不同 workspace 互不串扰）；
- unsubscribe 移除订阅；
- close 幂等：终止 async 迭代、重复 close 不抛、close 后 publish 为 noop；
- 空 workspace_id 校验。
"""

from __future__ import annotations

import asyncio

import pytest

from app.models.enums.event_type import EventType
from app.models.workspace_index_event import WorkspaceIndexEvent
from app.service.codegraph.workspace_index_bus import WorkspaceIndexBus


def _event(workspace_id: str = "ws-1") -> WorkspaceIndexEvent:
    return WorkspaceIndexEvent(
        event_type=EventType.WORKSPACE_READY,
        workspace_id=workspace_id,
        workspace_path="/ws/root",
        payload={"workspace_path": "/ws/root"},
    )


def test_empty_workspace_id_raises():
    bus = WorkspaceIndexBus()
    with pytest.raises(ValueError):
        bus.subscribe("")


def test_subscribe_routes_publish_by_workspace_id():
    bus = WorkspaceIndexBus()
    sub_a = bus.subscribe("ws-a")
    sub_b = bus.subscribe("ws-b")

    async def run():
        bus.publish(_event("ws-a"))
        event_a = await sub_a.__anext__()
        # ws-b 的订阅不应收到 ws-a 的事件
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(sub_b.__anext__(), timeout=0.05)
        return event_a

    event = asyncio.run(run())
    assert event.workspace_id == "ws-a"
    assert event.event_type == EventType.WORKSPACE_READY


def test_publish_only_to_registered_subscribers():
    bus = WorkspaceIndexBus()
    sub = bus.subscribe("ws-1")
    bus.unsubscribe(sub)
    # 已退订后 publish 不应投递，也不抛
    bus.publish(_event("ws-1"))

    async def run():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(sub.__anext__(), timeout=0.05)

    asyncio.run(run())


def test_unsubscribe_stops_delivery():
    bus = WorkspaceIndexBus()
    sub = bus.subscribe("ws-1")
    bus.unsubscribe(sub)
    # 退订后发布无投递（同 test_publish_only...），重复退订幂等
    bus.unsubscribe(sub)
    bus.publish(_event("ws-1"))

    async def run():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(sub.__anext__(), timeout=0.05)

    asyncio.run(run())


def test_close_terminates_async_iteration():
    bus = WorkspaceIndexBus()
    sub = bus.subscribe("ws-1")
    bus.close("ws-1")

    async def run():
        with pytest.raises(StopAsyncIteration):
            await sub.__anext__()

    asyncio.run(run())


def test_close_is_idempotent_and_publish_after_close_is_noop():
    bus = WorkspaceIndexBus()
    sub = bus.subscribe("ws-1")
    bus.close("ws-1")
    bus.close("ws-1")  # 重复 close 不抛
    bus.publish(_event("ws-1"))  # close 后 publish 为 noop，不抛

    async def run():
        with pytest.raises(StopAsyncIteration):
            await sub.__anext__()

    asyncio.run(run())


def test_queue_size_must_be_positive():
    with pytest.raises(ValueError):
        WorkspaceIndexBus(queue_size=0)


def test_event_rejects_non_workspace_index_type():
    """非 workspace 索引进度类型（如 RUN_STARTED）应被拒绝。"""
    from app.models.workspace_index_event import WorkspaceIndexEvent

    with pytest.raises(ValueError):
        WorkspaceIndexEvent(
            event_type=EventType.RUN_STARTED,
            workspace_id="ws-1",
            workspace_path="/ws",
            payload={},
        )


def test_publish_drops_oldest_when_queue_full():
    """队列满时 publish 应丢弃最旧事件并放入新事件（不抛异常）。"""
    bus = WorkspaceIndexBus(queue_size=2)
    sub = bus.subscribe("ws-1")

    async def run():
        # 填满 2 容量队列：3 个事件触发第 1 个被丢弃
        bus.publish(_event("ws-1"))
        bus.publish(_event("ws-1"))
        bus.publish(_event("ws-1"))
        # 队列应保留最后 2 个（丢弃最旧）
        first = await sub.__anext__()
        second = await sub.__anext__()
        return first, second

    first, second = asyncio.run(run())
    assert first.event_id != second.event_id
