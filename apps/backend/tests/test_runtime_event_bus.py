"""RuntimeEventBus 去重窗口与内存有界性的单元测试。"""

import asyncio

from app.models.enums.event_type import EventType
from app.models.event.runtime_event import RuntimeEvent
from app.models.payload.run_started_payload import RunStartedPayload
from app.service.agent_runtime_event.runtime_event_bus import RuntimeEventBus


def _make_event(turn_id: str, event_id: str) -> RuntimeEvent:
    """构造指定轮次与事件标识的运行开始事件。

    参数:
        turn_id: 事件所属轮次标识。
        event_id: 事件唯一标识，用于去重验证。

    返回:
        携带指定 turn_id 与 event_id 的 RuntimeEvent。

    异常:
        无。

    副作用:
        无。
    """

    return RuntimeEvent(
        event_type=EventType.RUN_STARTED,
        task_id="task_1",
        turn_id=turn_id,
        event_id=event_id,
        payload=RunStartedPayload(status="running", agent_id="developer"),
    )


async def test_publish_deduplicates_same_event_id():
    """验证同一 event_id 重复发布时只入队一次。

    参数:
        无。

    返回:
        无。

    异常:
        无。

    副作用:
        向订阅队列写入一个事件。
    """

    bus = RuntimeEventBus(queue_size=16)
    sub = bus.subscribe("turn-1")
    bus.publish(_make_event("turn-1", "evt-1"))
    bus.publish(_make_event("turn-1", "evt-1"))
    assert sub.queue.qsize() == 1


async def test_dedup_window_is_bounded_when_queue_drops_oldest():
    """验证去重窗口受队列容量限制：超窗最旧事件可重新入队，窗内事件仍被去重。

    参数:
        无。

    返回:
        无。

    异常:
        无。

    副作用:
        向订阅队列写入一个越窗事件。
    """

    bus = RuntimeEventBus(queue_size=4)
    sub = bus.subscribe("turn-1")
    for i in range(8):
        bus.publish(_make_event("turn-1", f"evt-{i}"))
    # 队列容量 4，消费掉现存事件以清空观察窗口。
    for _ in range(4):
        await sub.queue.get()
    # evt-0 已超出 4 条去重窗口，重复发布应重新入队。
    bus.publish(_make_event("turn-1", "evt-0"))
    try:
        got = sub.queue.get_nowait()
    except asyncio.QueueEmpty:
        got = None
    assert got is not None and got.event_id == "evt-0"
    # evt-7 仍在窗口内，重复发布应被去重。
    bus.publish(_make_event("turn-1", "evt-7"))
    assert sub.queue.empty()


def test_published_event_ids_set_is_bounded():
    """验证每个 turn 的去重集合大小受队列容量限制，不随发布量无限增长。

    参数:
        无。

    返回:
        无。

    异常:
        无。

    副作用:
        无。
    """

    bus = RuntimeEventBus(queue_size=4)
    for i in range(20):
        bus.publish(_make_event("turn-1", f"evt-{i}"))
    # 窗口在事件数超过容量后恒满（始终保留最近 4 个 ID）。
    assert len(bus._published_event_ids_by_turn["turn-1"]) == 4


def test_close_turn_clears_dedup_state():
    """验证 close_turn 后该 turn 的去重集合被清空。

    参数:
        无。

    返回:
        无。

    异常:
        无。

    副作用:
        无。
    """

    bus = RuntimeEventBus(queue_size=4)
    bus.publish(_make_event("turn-1", "evt-1"))
    assert "turn-1" in bus._published_event_ids_by_turn
    bus.close_turn("turn-1")
    assert "turn-1" not in bus._published_event_ids_by_turn
