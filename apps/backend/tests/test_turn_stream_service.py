"""TurnStreamService 单元测试（app/service/task/turn_stream_service.py）。

覆盖：正常流、RUN_FINISHED 后等待 producer 送达 file_change_stable、RUN_FAILED/RUN_CANCELLED
立即结束、断连兜底（run 未启动即取消落 failed / 启动后取消不兜底）、纯订阅转发、
events is None 关闭、异常路径（producer 异常 / subscription 异常 / update_turn_status 异常）、
producer 槽位释放。
"""

import asyncio
from datetime import UTC, datetime

import pytest

from app.models import TurnRecord
from app.models.enums.event_type import EventType
from app.models.event.runtime_event import RuntimeEvent
from app.models.payload.file_change_stable_payload import FileChangeStablePayload
from app.models.payload.run_cancelled_payload import RunCancelledPayload
from app.models.payload.run_failed_payload import RunFailedPayload
from app.models.payload.run_finished_payload import RunFinishedPayload
from app.models.payload.run_started_payload import RunStartedPayload
from app.service.agent_runtime_event.runtime_event_bus import RuntimeEventBus
from app.service.task.turn_stream_service import TurnStreamService

TURN_ID = "t1"
TASK_ID = "task_1"


class _TurnServiceStub:
    """记录 update_turn_status 调用；可配置抛异常。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.fail_update = False

    def update_turn_status(self, turn_id: str, status: str, end_reason: str | None = None) -> None:
        # 先记录调用，再按配置抛异常，以便断言「已被调用但失败」
        self.calls.append((turn_id, status, end_reason))
        if self.fail_update:
            raise RuntimeError("db down")


class _RuntimeEventServiceStub:
    """记录 save_and_publish 的事件。"""

    def __init__(self) -> None:
        self.saved: list[RuntimeEvent] = []

    def save_and_publish(self, event: RuntimeEvent) -> RuntimeEvent:
        self.saved.append(event)
        return event


class _SpyBus(RuntimeEventBus):
    """真实事件总线 + 调用记录（subscribe 时保存订阅句柄）。"""

    def __init__(self) -> None:
        super().__init__()
        self.claims = 0
        self.releases = 0
        self.closes = 0
        self.last_subscription = None

    def claim_turn_producer(self, turn_id: str) -> bool:
        self.claims += 1
        return super().claim_turn_producer(turn_id)

    def release_turn_producer(self, turn_id: str) -> None:
        self.releases += 1
        super().release_turn_producer(turn_id)

    def close_turn(self, turn_id: str) -> None:
        self.closes += 1
        super().close_turn(turn_id)

    def subscribe(self, turn_id: str):
        sub = super().subscribe(turn_id)
        self.last_subscription = sub
        return sub


class _NoClaimBus(RuntimeEventBus):
    """模拟本轮已有活跃 producer（claim 失败），仅订阅转发。"""

    def claim_turn_producer(self, turn_id: str) -> bool:
        return False


class _KeyErrorSubscription:
    """订阅首个事件即抛 KeyError（轮次在执行前消失）。"""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise KeyError(TURN_ID)


class _KeyErrorBus:
    """订阅返回抛 KeyError 的订阅、不认领 producer 的总线替身。"""

    def __init__(self) -> None:
        self.unsubscribed: list[object] = []

    def subscribe(self, turn_id: str):
        return _KeyErrorSubscription()

    def claim_turn_producer(self, turn_id: str) -> bool:
        return False

    def unsubscribe(self, subscription: object) -> None:
        self.unsubscribed.append(subscription)


def _make_event(event_type: EventType, turn_id: str = TURN_ID, task_id: str = TASK_ID) -> RuntimeEvent:
    """按事件类型构造匹配 payload 的 RuntimeEvent。"""

    if event_type is EventType.RUN_STARTED:
        payload = RunStartedPayload(status="running", agent_id="agent_1")
    elif event_type is EventType.RUN_FINISHED:
        payload = RunFinishedPayload(status="completed")
    elif event_type is EventType.RUN_FAILED:
        payload = RunFailedPayload(error="boom", status="failed")
    elif event_type is EventType.RUN_CANCELLED:
        payload = RunCancelledPayload(status="cancelled")
    elif event_type is EventType.FILE_CHANGE_STABLE:
        payload = FileChangeStablePayload(task_id=task_id, turn_id=turn_id, path="a.py", action="modified")
    else:
        raise ValueError(f"unsupported test event type: {event_type}")
    return RuntimeEvent(event_type=event_type, task_id=task_id, turn_id=turn_id, payload=payload)


def _turn(status: str = "pending") -> TurnRecord:
    now = datetime.now(UTC)
    return TurnRecord(
        turn_id=TURN_ID,
        task_id=TASK_ID,
        input_text="hello",
        status=status,
        created_at=now,
        updated_at=now,
    )


def _make_service(bus: RuntimeEventBus, turn_svc: _TurnServiceStub | None = None):
    return TurnStreamService(bus, turn_svc or _TurnServiceStub(), _RuntimeEventServiceStub())


# --- 场景 1：正常流 RUN_STARTED → RUN_FINISHED，producer 槽位释放 ---
async def test_stream_happy_path_releases_producer():
    """正常流收集完整事件序列且 producer 槽位释放（可再次 claim）。"""
    bus = _SpyBus()
    svc = _make_service(bus)
    turn = _turn()
    started = _make_event(EventType.RUN_STARTED)
    finished = _make_event(EventType.RUN_FINISHED)

    async def runner(_t):
        async def gen():
            yield started
            yield finished

        return gen()

    collected = [e async for e in svc.stream_turn_events(runner, turn)]

    assert [e.event_type for e in collected] == [EventType.RUN_STARTED, EventType.RUN_FINISHED]
    assert bus.claims >= 1
    assert bus.claim_turn_producer(TURN_ID) is True  # 槽位已释放


# --- 场景 2：RUN_FINISHED 后 producer 仍产出 file_change_stable，等待 producer 送达 ---
async def test_stream_delivers_file_change_stable_after_run_finished():
    """RUN_FINISHED 后等待 producer 结束，file_change_stable 增量事件仍送达。"""
    bus = _SpyBus()
    svc = _make_service(bus)
    turn = _turn()
    started = _make_event(EventType.RUN_STARTED)
    finished = _make_event(EventType.RUN_FINISHED)
    stable = _make_event(EventType.FILE_CHANGE_STABLE)

    async def runner(_t):
        async def gen():
            yield started
            yield finished
            yield stable

        return gen()

    collected = [e async for e in svc.stream_turn_events(runner, turn)]

    assert [e.event_type for e in collected] == [
        EventType.RUN_STARTED,
        EventType.RUN_FINISHED,
        EventType.FILE_CHANGE_STABLE,
    ]
    assert bus.closes >= 1


# --- 场景 3：RUN_FAILED / RUN_CANCELLED 立即结束（break 语义）---
async def test_stream_stops_immediately_on_run_failed():
    """RUN_FAILED 终态后立即结束，其后的增量事件不送达（break 语义）。"""
    bus = _SpyBus()
    svc = _make_service(bus)
    turn = _turn()
    started = _make_event(EventType.RUN_STARTED)
    failed = _make_event(EventType.RUN_FAILED)
    stable = _make_event(EventType.FILE_CHANGE_STABLE)

    async def runner(_t):
        async def gen():
            yield started
            yield failed
            yield stable

        return gen()

    collected = [e async for e in svc.stream_turn_events(runner, turn)]

    assert [e.event_type for e in collected] == [EventType.RUN_STARTED, EventType.RUN_FAILED]
    assert EventType.FILE_CHANGE_STABLE not in [e.event_type for e in collected]


async def test_stream_stops_immediately_on_run_cancelled():
    """RUN_CANCELLED 终态后立即结束（break 语义）。"""
    bus = _SpyBus()
    svc = _make_service(bus)
    turn = _turn()
    started = _make_event(EventType.RUN_STARTED)
    cancelled = _make_event(EventType.RUN_CANCELLED)

    async def runner(_t):
        async def gen():
            yield started
            yield cancelled
            await asyncio.sleep(0.02)

        return gen()

    collected = [e async for e in svc.stream_turn_events(runner, turn)]

    assert [e.event_type for e in collected] == [EventType.RUN_STARTED, EventType.RUN_CANCELLED]


# --- 场景 4：断连兜底（_drive_runtime_turn 直接单测）---
async def test_drive_runtime_turn_fallback_when_cancelled_before_run_entered():
    """runner 在 await 前抛 CancelledError（entered_run 未置位）→ 落 failed + 补发 RUN_FAILED。"""
    bus = _SpyBus()
    turn_svc = _TurnServiceStub()
    evt_svc = _RuntimeEventServiceStub()
    svc = TurnStreamService(bus, turn_svc, evt_svc)
    turn = _turn()

    async def runner(_t):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await svc._drive_runtime_turn(runner, turn)

    assert turn_svc.calls == [(TURN_ID, "failed", "client_disconnected")]
    assert len(evt_svc.saved) == 1
    failed = evt_svc.saved[0]
    assert failed.event_type is EventType.RUN_FAILED
    assert failed.turn_id == TURN_ID
    assert failed.payload.error == "client_disconnected"
    assert failed.payload.status == "failed"
    assert bus.releases >= 1


async def test_drive_runtime_turn_no_fallback_when_cancelled_after_run_entered():
    """runner 正常返回后（entered_run 置位）取消 → 不触发本地兜底（交 run_turn 内部处理）。"""
    bus = _SpyBus()
    turn_svc = _TurnServiceStub()
    evt_svc = _RuntimeEventServiceStub()
    svc = TurnStreamService(bus, turn_svc, evt_svc)
    turn = _turn()
    started = _make_event(EventType.RUN_STARTED)

    async def runner(_t):
        async def gen():
            try:
                yield started
                while True:
                    await asyncio.sleep(3600)
            finally:
                return

        return gen()

    task = asyncio.create_task(svc._drive_runtime_turn(runner, turn))
    await asyncio.sleep(0.05)  # 让 runner 返回、entered_run 置位并进入消费
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert turn_svc.calls == []
    assert evt_svc.saved == []
    assert bus.releases >= 1


# --- 场景 5：纯订阅转发，不认领 producer ---
async def test_stream_subscribed_turn_events_forwards_without_claiming():
    """纯订阅转发实时事件，不认领 producer、不释放槽位。"""
    bus = _SpyBus()
    svc = _make_service(bus)
    evt1 = _make_event(EventType.RUN_STARTED)
    evt2 = _make_event(EventType.FILE_CHANGE_STABLE)

    async def consume():
        return [e async for e in svc.stream_subscribed_turn_events(TURN_ID)]

    async def publish():
        await asyncio.sleep(0.01)
        bus.publish(evt1)
        bus.publish(evt2)
        bus.close_turn(TURN_ID)

    collected, _ = await asyncio.gather(consume(), publish())

    assert [e.event_type for e in collected] == [EventType.RUN_STARTED, EventType.FILE_CHANGE_STABLE]
    assert bus.claims == 0
    assert bus.releases == 0


# --- 场景 6：events is None → close_turn，不触发兜底 ---
async def test_drive_runtime_turn_none_events_closes_turn_without_fallback():
    """runner 返回 None → close_turn 关闭订阅、不落 failed。"""
    bus = _SpyBus()
    turn_svc = _TurnServiceStub()
    svc = TurnStreamService(bus, turn_svc, _RuntimeEventServiceStub())
    turn = _turn()

    async def runner(_t):
        return None

    await svc._drive_runtime_turn(runner, turn)

    assert bus.closes >= 1
    assert bus.releases >= 1
    assert turn_svc.calls == []


async def test_stream_none_events_yields_nothing_and_releases_producer():
    """stream 级：runner 返回 None → 无事件产出、producer 槽位释放。"""
    bus = _SpyBus()
    svc = _make_service(bus)
    turn = _turn()

    async def runner(_t):
        return None

    collected = [e async for e in svc.stream_turn_events(runner, turn)]

    assert collected == []
    assert bus.claims >= 1
    assert bus.claim_turn_producer(TURN_ID) is True


# --- 场景 7：异常路径 ---
async def test_stream_swallows_producer_exception():
    """producer 中途抛异常 → 流正常结束且不向上抛。"""
    bus = _SpyBus()
    svc = _make_service(bus)
    turn = _turn()
    started = _make_event(EventType.RUN_STARTED)

    async def runner(_t):
        async def gen():
            yield started
            raise RuntimeError("runner exploded")

        return gen()

    collected = [e async for e in svc.stream_turn_events(runner, turn)]

    assert [e.event_type for e in collected] == [EventType.RUN_STARTED]


async def test_stream_swallows_subscription_exception():
    """订阅消费中途抛异常（注入非法对象）→ 记录日志终止流，不向上抛。"""
    bus = _SpyBus()
    svc = _make_service(bus)
    turn = _turn()
    started = _make_event(EventType.RUN_STARTED)

    async def runner(_t):
        async def gen():
            yield started

        return gen()

    stream = svc.stream_turn_events(runner, turn)
    agen = stream.__aiter__()
    first = await agen.__anext__()
    assert first.event_type is EventType.RUN_STARTED

    await asyncio.sleep(0.02)
    bus.last_subscription.queue.put_nowait(object())  # 非法对象 → __anext__ 抛 TypeError
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()


async def test_stream_cancels_still_running_producer_on_subscription_error():
    """订阅异常且 producer 仍在运行 → finally 中 cancel producer 并释放槽位。"""
    bus = _SpyBus()
    svc = _make_service(bus)
    turn = _turn()
    started = _make_event(EventType.RUN_STARTED)

    async def runner(_t):
        async def gen():
            try:
                yield started
                while True:
                    await asyncio.sleep(3600)
            finally:
                return

        return gen()

    stream = svc.stream_turn_events(runner, turn)
    agen = stream.__aiter__()
    first = await agen.__anext__()
    assert first.event_type is EventType.RUN_STARTED

    bus.last_subscription.queue.put_nowait(object())
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()

    await asyncio.sleep(0.05)
    assert bus.releases >= 1


async def test_drive_runtime_turn_update_status_exception_preserves_cancel():
    """update_turn_status 抛异常 → 记录日志不中断，CancelledError 仍向上传播。"""
    bus = _SpyBus()
    turn_svc = _TurnServiceStub()
    turn_svc.fail_update = True
    evt_svc = _RuntimeEventServiceStub()
    svc = TurnStreamService(bus, turn_svc, evt_svc)
    turn = _turn()

    async def runner(_t):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await svc._drive_runtime_turn(runner, turn)

    # update_turn_status 被调用（虽抛异常）；_emit_run_failed 未执行（被异常打断）
    assert turn_svc.calls == [(TURN_ID, "failed", "client_disconnected")]
    assert evt_svc.saved == []


async def test_stream_swallows_key_error_from_subscription():
    """订阅首个事件即抛 KeyError（轮次消失）→ 记录日志终止流并退订。"""
    bus = _KeyErrorBus()
    svc = _make_service(bus)
    turn = _turn()

    collected = [e async for e in svc.stream_turn_events(_key_error_runner, turn)]

    assert collected == []
    assert len(bus.unsubscribed) == 1


async def test_stream_run_finished_without_producer_breaks():
    """claim 失败（producer 为 None）时收到 RUN_FINISHED → break 结束，不启动 producer。"""
    bus = _NoClaimBus()
    svc = _make_service(bus)
    turn = _turn()
    started = _make_event(EventType.RUN_STARTED)
    finished = _make_event(EventType.RUN_FINISHED)
    runner_called = False

    async def runner(_t):
        nonlocal runner_called
        runner_called = True

        async def gen():
            yield started
            yield finished

        return gen()

    async def external_producer():
        await asyncio.sleep(0.01)
        bus.publish(started)
        bus.publish(finished)
        bus.close_turn(TURN_ID)

    async def consume():
        return [e async for e in svc.stream_turn_events(runner, turn)]

    collected, _ = await asyncio.gather(consume(), external_producer())

    assert [e.event_type for e in collected] == [EventType.RUN_STARTED, EventType.RUN_FINISHED]
    assert runner_called is False  # 未认领 producer → runner 不被调用


async def test_emit_run_failed_with_none_turn_uses_empty_task_id():
    """_emit_run_failed 收到 turn=None → task_id 用空串，仍落库发布 RUN_FAILED。"""
    evt_svc = _RuntimeEventServiceStub()
    svc = TurnStreamService(_SpyBus(), _TurnServiceStub(), evt_svc)

    svc._emit_run_failed(None, TURN_ID)

    assert len(evt_svc.saved) == 1
    failed = evt_svc.saved[0]
    assert failed.event_type is EventType.RUN_FAILED
    assert failed.task_id == ""
    assert failed.payload.error == "client_disconnected"
    # M1 回归：断连兜底路径必须把 end_reason 填为 client_disconnected，
    # 否则前端 StatusBadge 永远读到 undefined、无法区分断开与真失败。
    assert failed.payload.end_reason == "client_disconnected"


async def test_emit_run_failed_with_real_turn_sets_end_reason():
    """_emit_run_failed 收到真实 turn → task_id 取自 turn 且 end_reason 仍为 client_disconnected。

    潜在缺陷类型：若实现把 end_reason 只在 turn=None 分支填充（或 task_id 取错来源），
    真实断连场景（绝大多数生产路径）前端仍拿不到 client_disconnected。
    """
    evt_svc = _RuntimeEventServiceStub()
    svc = TurnStreamService(_SpyBus(), _TurnServiceStub(), evt_svc)

    svc._emit_run_failed(_turn(), TURN_ID)

    assert len(evt_svc.saved) == 1
    failed = evt_svc.saved[0]
    assert failed.event_type is EventType.RUN_FAILED
    assert failed.task_id == TASK_ID
    assert failed.turn_id == TURN_ID
    assert failed.payload.error == "client_disconnected"
    assert failed.payload.status == "failed"
    assert failed.payload.end_reason == "client_disconnected"


async def test_run_failed_payload_end_reason_defaults_to_none():
    """RunFailedPayload 仅传 error/status → end_reason 默认 None（向后兼容）。

    潜在缺陷类型：若新增字段被设为必填或默认值写成 "client_disconnected"，
    runner 真异常分支会被误判为客户端断开。
    """
    payload = RunFailedPayload(error="boom", status="failed")

    assert payload.end_reason is None
    assert payload.error == "boom"
    assert payload.status == "failed"


async def _key_error_runner(_t):
    """配合 _KeyErrorBus 的 runner（不会被调用，占位）。"""
    return None
