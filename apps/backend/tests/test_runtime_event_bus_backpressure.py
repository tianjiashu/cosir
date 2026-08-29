"""``RuntimeEventBus`` 背压淘汰回归测试（D2 修复验证）。

回归覆盖：队列满时优先丢弃最旧的非终态事件，确保终态事件（RUN_FINISHED 等）
与关闭哨兵始终入队。否则订阅者会在收到关闭哨兵后提前结束流，前端收不到完整
终态（turn_stream_service 的 terminal_received 永不置位）。

测试用真实 ``RuntimeEvent`` + 小容量 ``asyncio.Queue`` 模拟满队列，不依赖真实 DB。
"""

from asyncio import Queue, QueueFull

from app.models.enums.event_type import EventType
from app.models.event.runtime_event import RuntimeEvent
from app.models.payload.model_output_delta_payload import ModelOutputDeltaPayload
from app.models.payload.run_finished_payload import RunFinishedPayload
from app.service.agent_runtime_event.runtime_event_bus import RuntimeEventBus
from app.service.agent_runtime_event.runtime_event_subscription import _QUEUE_CLOSED


def _delta(turn_id: int, seq: int) -> RuntimeEvent:
    """构造一个非终态的模型输出增量事件。"""

    return RuntimeEvent(
        event_type=EventType.MODEL_OUTPUT_DELTA,
        task_id=1,
        turn_id=turn_id,
        sequence=seq,
        payload=ModelOutputDeltaPayload(step_id="s1", text=f"chunk-{seq}"),
    )


def _finished(turn_id: int) -> RuntimeEvent:
    """构造一个终态事件 RUN_FINISHED。"""

    return RuntimeEvent(
        event_type=EventType.RUN_FINISHED,
        task_id=1,
        turn_id=turn_id,
        sequence=999,
        payload=RunFinishedPayload(status="completed"),
    )


def test_terminal_event_kept_when_queue_full() -> None:
    """队列满且全为非终态时，发布 RUN_FINISHED 必须入队（最旧非终态被挤出）。"""

    bus = RuntimeEventBus(queue_size=4)
    sub = bus.subscribe(1)
    # 填满 4 个非终态事件
    for i in range(4):
        bus.publish(_delta(1, i))

    # 再发布一个非终态，应挤出最旧非终态（维持容量）
    bus.publish(_delta(1, 100))
    non_terminal_after = [
        e for e in list(sub.queue._queue) if isinstance(e, RuntimeEvent)
    ]
    assert len(non_terminal_after) == 4

    # 发布终态事件：最旧非终态被挤出，终态必须保留
    bus.publish(_finished(1))
    queued = list(sub.queue._queue)
    terminal_present = any(
        isinstance(e, RuntimeEvent) and e.event_type == EventType.RUN_FINISHED
        for e in queued
    )
    assert terminal_present, "RUN_FINISHED 终态事件未入队，D2 修复失效"


def test_non_terminal_event_drops_oldest_when_queue_full() -> None:
    """发布普通事件且队列满时，维持原背压语义：丢弃最旧事件。"""

    bus = RuntimeEventBus(queue_size=2)
    sub = bus.subscribe(1)
    bus.publish(_delta(1, 1))
    bus.publish(_delta(1, 2))

    bus.publish(_delta(1, 3))
    queued = [e for e in list(sub.queue._queue) if isinstance(e, RuntimeEvent)]
    assert len(queued) == 2
    # 最旧的 seq=1 应被丢弃
    seqs = sorted(e.sequence for e in queued)
    assert seqs == [2, 3]


def test_close_turn_sentinel_enqueued_when_full() -> None:
    """队列满时 close_turn 的关闭哨兵必须入队，否则流永不结束。"""

    bus = RuntimeEventBus(queue_size=2)
    sub = bus.subscribe(1)
    bus.publish(_delta(1, 1))
    bus.publish(_delta(1, 2))

    bus.close_turn(1)
    queued = list(sub.queue._queue)
    assert _QUEUE_CLOSED in queued, "关闭哨兵未入队，订阅者流无法结束"
