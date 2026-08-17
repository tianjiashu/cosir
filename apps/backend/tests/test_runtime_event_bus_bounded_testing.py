"""RuntimeEventBus 有界去重窗口（FIFO OrderedDict）专项边界测试。

仅覆盖去重窗口的有界语义本身，不重复已有 test_runtime_event_bus.py 的常规路径。
"""

import pytest

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


async def _drain(sub, count: int) -> list[RuntimeEvent]:
    """从订阅队列中消费指定数量的事件。

    参数:
        sub: 订阅对象。
        count: 需要消费的事件数量。

    返回:
        按入队顺序取出的事件列表。

    异常:
        无。

    副作用:
        消费订阅队列中的事件。
    """

    out = []
    for _ in range(count):
        out.append(await sub.queue.get())
    return out


def test_queue_size_1_window_evicts_oldest_immediately():
    """边界：queue_size=1 时去重窗口仅含 1 个 event_id，最旧立即淘汰。

    可能发现的缺陷类型：窗口容量计算错误（如容量为 0 或 >=2）、
    淘汰逻辑用错 key/弹出方向，导致窗口大小不是严格的 1。
    """
    bus = RuntimeEventBus(queue_size=1)
    bus.publish(_make_event("turn-1", "a"))
    # 发布第二个事件后窗口应只保留 {b}，{a} 被立即淘汰。
    bus.publish(_make_event("turn-1", "b"))

    window = bus._published_event_ids_by_turn["turn-1"]
    assert list(window.keys()) == ["b"]
    assert len(window) == 1

    # 重复发布已淘汰的 a 应重新入队（不再被去重）。
    bus.publish(_make_event("turn-1", "a"))
    assert list(bus._published_event_ids_by_turn["turn-1"].keys()) == ["a"]


def test_exactly_queue_size_events_all_dedup_no_eviction():
    """边界：事件量恰好等于 queue_size 时，窗口内全部去重、无淘汰。

    可能发现的缺陷类型：淘汰条件写成 >= 而非 >，导致恰好满窗时误淘汰最旧事件；
    去重判断失误导致同 ID 重复入队。
    """
    bus = RuntimeEventBus(queue_size=3)
    sub = bus.subscribe("turn-1")
    for i in range(3):
        bus.publish(_make_event("turn-1", f"evt-{i}"))

    # 恰好 3 个事件，窗口应完整保留且顺序为发布序。
    assert list(bus._published_event_ids_by_turn["turn-1"].keys()) == [
        "evt-0",
        "evt-1",
        "evt-2",
    ]

    # 满窗不淘汰：重复发布最早 evt-0 仍被去重，不入队。
    bus.publish(_make_event("turn-1", "evt-0"))
    assert sub.queue.qsize() == 3


def test_turns_are_isolated_each_has_own_window():
    """跨 turn 隔离：不同 turn 的去重窗口互不影响，各自窗内去重与超窗淘汰独立。

    可能发现的缺陷类型：去重集合被多个 turn 共享（如用全局 set），
    导致跨 turn 误去重或窗口容量被跨 turn 事件挤占。
    """
    bus = RuntimeEventBus(queue_size=2)
    bus.publish(_make_event("turn-A", "x1"))
    bus.publish(_make_event("turn-B", "y1"))

    # turn-A 填满自身窗口。
    bus.publish(_make_event("turn-A", "x2"))
    # turn-B 仍只有 1 个，窗口各自独立。
    assert list(bus._published_event_ids_by_turn["turn-A"].keys()) == ["x1", "x2"]
    assert list(bus._published_event_ids_by_turn["turn-B"].keys()) == ["y1"]

    # 向 turn-A 发布第三个事件：仅 turn-A 最旧 x1 淘汰，turn-B 不受影响。
    bus.publish(_make_event("turn-A", "x3"))
    assert list(bus._published_event_ids_by_turn["turn-A"].keys()) == ["x2", "x3"]
    assert list(bus._published_event_ids_by_turn["turn-B"].keys()) == ["y1"]

    # turn-B 仍能重复去重其窗口内 y1。
    sub_b = bus.subscribe("turn-B")
    bus.publish(_make_event("turn-B", "y1"))
    assert sub_b.queue.qsize() == 0


def test_queue_size_below_one_raises_value_error():
    """异常路径：queue_size<1 抛 ValueError（验证既有行为未被破坏）。

    可能发现的缺陷类型：构造函数校验条件被改动（如改为 <=0 或放宽为 0 允许），
    或抛错类型变更。
    """
    for bad in (0, -1, -100):
        with pytest.raises(ValueError):
            RuntimeEventBus(queue_size=bad)


def test_fifo_eviction_order_matches_publish_order():
    """并发/顺序：publish 顺序与淘汰顺序一致（FIFO 严格性）。

    可能发现的缺陷类型：淘汰非最旧元素（如 popitem(last=True) 弹新端），
    或 OrderedDict 未按插入序维护导致淘汰次序错乱。
    """
    bus = RuntimeEventBus(queue_size=3)
    # 发布 10 个唯一事件，持续触发淘汰。
    for i in range(10):
        bus.publish(_make_event("turn-1", f"e{i}"))

    window = bus._published_event_ids_by_turn["turn-1"]
    assert len(window) == 3
    # 最终窗口应保留最后 3 个事件，且按发布序排列（FIFO）。
    assert list(window.keys()) == ["e7", "e8", "e9"]

    # 依序发布新唯一事件，验证淘汰顺序严格 FIFO：每发布一个新事件，
    # 当前窗口最旧（最早幸存）者被淘汰，窗口始终有界为 queue_size。
    # 发布 e10 后：e7 被淘汰，窗口 [e8,e9,e10]。
    bus.publish(_make_event("turn-1", "e10"))
    assert list(bus._published_event_ids_by_turn["turn-1"].keys()) == ["e8", "e9", "e10"]
    # 发布 e11 后：e8 被淘汰，窗口 [e9,e10,e11]。
    bus.publish(_make_event("turn-1", "e11"))
    assert list(bus._published_event_ids_by_turn["turn-1"].keys()) == ["e9", "e10", "e11"]
    # 已淘汰的 e7 重新发布：作为新事件进入窗口，并淘汰当前最旧 e9，
    # 窗口始终保持有界 3 且按"进入窗口"的先后序 FIFO 淘汰。
    bus.publish(_make_event("turn-1", "e7"))
    assert list(bus._published_event_ids_by_turn["turn-1"].keys()) == ["e10", "e11", "e7"]
