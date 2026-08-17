"""WorkspaceEventBus 单元测试。

守护：
1. P1-6：publish/close 可跨线程调用（prepare 经 asyncio.to_thread 在工作线程执行），
   经 ``loop.call_soon_threadsafe`` 调度到订阅者所属 loop 线程投递，不违反
   asyncio.Queue 非线程安全契约；多订阅者分属不同 loop 时逐订阅者投递。
2. P1-7：close 只关闭当前订阅桶、不写永久关闭标记；close 后再次 subscribe +
   publish 事件能正常送达（原 `_closed` 只增不减导致二次 prepare 事件被吞）。
3. 基本语义：发布送达、关闭哨兵、退订移除、订阅必须在 running loop 上下文。
"""

import asyncio
import threading

from app.models.enums.event_type import EventType
from app.models.event.workspace_event import WorkspaceEvent
from app.service.workspace_event.workspace_event_bus import WorkspaceEventBus
from app.service.workspace_event.workspace_event_subscription import _QUEUE_CLOSED


def _make_event(workspace_id: str) -> WorkspaceEvent:
    """构造一个 WORKSPACE_PREPARING 事件（默认事件负载）。

    参数:
        workspace_id: 事件所属 workspace 标识。

    返回:
        WorkspaceEvent 值对象。

    异常:
        无。

    副作用:
        无。
    """

    return WorkspaceEvent(
        event_type=EventType.WORKSPACE_PREPARING,
        workspace_id=workspace_id,
        workspace_path=f"/ws/{workspace_id}",
        payload={},
    )


async def test_publish_delivers_to_subscriber() -> None:
    """同 loop 线程发布：订阅者队列收到事件。"""

    bus = WorkspaceEventBus(queue_size=8)
    sub = bus.subscribe("ws-1")
    event = _make_event("ws-1")
    bus.publish(event)
    got = await asyncio.wait_for(sub.queue.get(), timeout=1)
    assert got is event


async def test_publish_from_worker_thread_delivers() -> None:
    """P1-6：工作线程（asyncio.to_thread，prepare 场景）发布可送达 loop 订阅者。"""

    bus = WorkspaceEventBus(queue_size=8)
    sub = bus.subscribe("ws-1")
    event = _make_event("ws-1")
    # prepare 在 API 端点经 asyncio.to_thread 抛到工作线程执行，publish 跨线程。
    await asyncio.to_thread(bus.publish, event)
    got = await asyncio.wait_for(sub.queue.get(), timeout=1)
    assert got is event


async def test_close_sentinel_delivered() -> None:
    """同 loop 线程 close：订阅者队列收到关闭哨兵。"""

    bus = WorkspaceEventBus(queue_size=8)
    sub = bus.subscribe("ws-1")
    bus.close("ws-1")
    got = await asyncio.wait_for(sub.queue.get(), timeout=1)
    assert got is _QUEUE_CLOSED


async def test_close_from_worker_thread_delivers_sentinel() -> None:
    """P1-6：工作线程 close（prepare 的 finally 场景）哨兵可送达 loop 订阅者。"""

    bus = WorkspaceEventBus(queue_size=8)
    sub = bus.subscribe("ws-1")
    await asyncio.to_thread(bus.close, "ws-1")
    got = await asyncio.wait_for(sub.queue.get(), timeout=1)
    assert got is _QUEUE_CLOSED


async def test_close_then_resubscribe_receives_events() -> None:
    """P1-7：close 后再次 subscribe + publish 事件能正常送达（无永久 _closed）。"""

    bus = WorkspaceEventBus(queue_size=8)
    first = bus.subscribe("ws-1")
    bus.close("ws-1")
    sentinel = await asyncio.wait_for(first.queue.get(), timeout=1)
    assert sentinel is _QUEUE_CLOSED

    # 二次 prepare：重新订阅后发布必须送达，而不是被旧 _closed 标记吞掉。
    second = bus.subscribe("ws-1")
    event = _make_event("ws-1")
    bus.publish(event)
    got = await asyncio.wait_for(second.queue.get(), timeout=1)
    assert got is event


async def test_close_then_resubscribe_after_worker_publish() -> None:
    """P1-6 + P1-7 组合：close 后工作线程二次 prepare 的事件不被吞。"""

    bus = WorkspaceEventBus(queue_size=8)
    first = bus.subscribe("ws-1")
    await asyncio.to_thread(bus.close, "ws-1")
    sentinel = await asyncio.wait_for(first.queue.get(), timeout=1)
    assert sentinel is _QUEUE_CLOSED

    second = bus.subscribe("ws-1")
    event = _make_event("ws-1")
    await asyncio.to_thread(bus.publish, event)
    got = await asyncio.wait_for(second.queue.get(), timeout=1)
    assert got is event


async def test_unsubscribe_removes_subscriber() -> None:
    """退订后该订阅者不再收到事件。"""

    bus = WorkspaceEventBus(queue_size=8)
    sub = bus.subscribe("ws-1")
    bus.unsubscribe(sub)
    bus.publish(_make_event("ws-1"))
    assert sub.queue.empty()


async def test_multiple_subscribers_all_receive() -> None:
    """同一 workspace 多订阅者（同 loop）都收到事件。"""

    bus = WorkspaceEventBus(queue_size=8)
    sub_a = bus.subscribe("ws-1")
    sub_b = bus.subscribe("ws-1")
    event = _make_event("ws-1")
    bus.publish(event)
    got_a = await asyncio.wait_for(sub_a.queue.get(), timeout=1)
    got_b = await asyncio.wait_for(sub_b.queue.get(), timeout=1)
    assert got_a is event
    assert got_b is event


def test_publish_to_subscriber_in_another_loop() -> None:
    """P1-6 多 loop：订阅者分属另一线程的 loop 时，发布经 call_soon_threadsafe 送达。"""

    bus = WorkspaceEventBus(queue_size=8)
    received: list[object] = []
    registered = threading.Event()
    done = threading.Event()

    def consumer_loop() -> None:
        """在独立线程运行第二个 event loop，订阅 ws-1 并等待一条事件。"""

        async def run() -> None:
            sub = bus.subscribe("ws-1")
            registered.set()
            try:
                item = await asyncio.wait_for(sub.queue.get(), timeout=2)
                received.append(item)
            finally:
                done.set()

        asyncio.run(run())

    consumer = threading.Thread(target=consumer_loop, daemon=True)
    consumer.start()
    assert registered.wait(2), "订阅者 loop 未在限定时间内完成订阅"

    # 主线程（无 running loop）发布：应投递到消费者线程的 loop。
    event = _make_event("ws-1")
    bus.publish(event)

    assert done.wait(2), "另一个 loop 的订阅者未在限定时间内收到事件"
    consumer.join(timeout=2)
    assert received == [event]


def test_subscribe_requires_running_loop() -> None:
    """订阅必须发生在 running loop 上下文（无 loop 时明确报错而非静默损坏）。"""

    bus = WorkspaceEventBus(queue_size=8)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        # 当前线程意外有 running loop（如 pytest-asyncio 环境），改用子线程验证。
        def _sync_subscribe() -> None:
            bus.subscribe("ws-1")

        thread = threading.Thread(target=_sync_subscribe)
        thread.start()
        thread.join()
        return
    try:
        bus.subscribe("ws-1")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError when subscribing without a running loop")
