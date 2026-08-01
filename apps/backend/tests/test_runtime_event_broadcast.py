"""Runtime event broadcast behavior tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest

from app.api.turns_api import _sse_turn_events
from app.models.enums.event_type import EventType
from app.models.payload import RunCancelledPayload
from app.models.runtime_event import RuntimeEvent
from app.service.runtime_event.runtime_event_bus import RuntimeEventBus
from app.service.runtime_event.runtime_event_service import RuntimeEventService


class _RecordingRuntimeEventCrud:
    """Runtime event CRUD fake that assigns a deterministic sequence."""

    def __init__(self) -> None:
        """Initialize the fake persistence state.

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            创建内存事件列表。
        """

        self.saved_events: list[dict[str, Any]] = []

    def save_event_with_next_sequence(self, event_dict: dict[str, Any]) -> int:
        """Save an event dictionary and return the assigned sequence.

        参数:
            event_dict: 待保存的事件字典。

        返回:
            本 fake 分配的 turn-local sequence。

        异常:
            无。

        副作用:
            追加事件字典到内存列表。
        """

        sequence = len(self.saved_events)
        event_dict["sequence"] = sequence
        self.saved_events.append(event_dict)
        return sequence


class _NoopRuntime:
    """Runtime fake whose run_turn stays alive until the SSE generator closes it."""

    async def run_turn(self, turn_id: str, turn=None):
        """Yield no events while keeping the async generator open.

        参数:
            turn_id: 运行轮次标识，本 fake 不使用。
            turn: 可选 turn 记录，本 fake 不使用。

        生成:
            无事件。

        异常:
            无。

        副作用:
            持续等待直到调用方关闭生成器。
        """

        await asyncio.Event().wait()
        yield RuntimeEvent(
            event_type=EventType.RUN_CANCELLED,
            task_id="task-1",
            turn_id=turn_id,
            payload=RunCancelledPayload(status="cancelled"),
        )  # pragma: no cover


class _YieldingRuntime:
    """Runtime fake that only yields events through the run_turn contract."""

    async def run_turn(self, turn_id: str, turn=None):
        """Yield a terminal event without publishing it to any bus.

        参数:
            turn_id: 运行轮次标识。
            turn: 可选 turn 记录，本 fake 不使用。

        生成:
            一个 run_cancelled 事件。

        异常:
            无。

        副作用:
            无；特意不触碰 RuntimeEventBus，用于验证 SSE 不能依赖 runtime 内部广播副作用。
        """

        yield RuntimeEvent(
            event_type=EventType.RUN_CANCELLED,
            task_id="task-1",
            turn_id=turn_id,
            payload=RunCancelledPayload(status="cancelled"),
        )


class _BlockingRuntime:
    """Runtime fake that records how many producers started for one turn."""

    def __init__(self) -> None:
        """Initialize runtime call counters and synchronization events.

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            创建测试用内存计数器和 asyncio 事件。
        """

        self.run_count = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run_turn(self, turn_id: str, turn=None):
        """Block until released, then yield a terminal event.

        参数:
            turn_id: 运行轮次标识。
            turn: 可选 turn 记录，本 fake 不使用。

        生成:
            release 后产出一个 run_cancelled 事件。

        异常:
            无。

        副作用:
            递增 run_count 并通知测试 producer 已启动。
        """

        self.run_count += 1
        self.started.set()
        await self.release.wait()
        yield RuntimeEvent(
            event_type=EventType.RUN_CANCELLED,
            task_id="task-1",
            turn_id=turn_id,
            payload=RunCancelledPayload(status="cancelled"),
        )


@pytest.mark.asyncio
async def test_runtime_event_service_saves_and_publishes_to_subscriber() -> None:
    """Saving a runtime event must publish the stamped event to turn subscribers."""

    crud = _RecordingRuntimeEventCrud()
    bus = RuntimeEventBus()
    service = RuntimeEventService(crud, bus)  # type: ignore[arg-type]
    subscription = bus.subscribe("turn-1")
    event = RuntimeEvent(
        event_type=EventType.RUN_CANCELLED,
        task_id="task-1",
        turn_id="turn-1",
        payload=RunCancelledPayload(status="cancelled"),
    )

    try:
        stamped = service.save_and_publish(event)
        delivered = await asyncio.wait_for(subscription.__anext__(), timeout=1)
    finally:
        bus.unsubscribe(subscription)

    assert stamped.sequence == 0
    assert delivered.event_id == stamped.event_id
    assert delivered.sequence == 0
    assert crud.saved_events[0]["event_id"] == stamped.event_id


@pytest.mark.asyncio
async def test_sse_stream_receives_cancel_event_published_outside_run_turn() -> None:
    """SSE subscribers must receive cancel events published by the cancel API path."""

    bus = RuntimeEventBus()
    stream = cast(
        AsyncGenerator[str, None],
        _sse_turn_events(
            runtime=_NoopRuntime(),  # type: ignore[arg-type]
            turn_id="turn-1",
            event_bus=bus,
        ),
    )
    first_frame = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0)
    event = RuntimeEvent(
        event_type=EventType.RUN_CANCELLED,
        task_id="task-1",
        turn_id="turn-1",
        payload=RunCancelledPayload(status="cancelled"),
    )

    bus.publish(event)
    frame = await asyncio.wait_for(first_frame, timeout=1)
    await stream.aclose()

    assert frame.startswith("event: run_cancelled\n")
    data = json.loads(frame.split("data: ", 1)[1])
    assert data["event_type"] == "run_cancelled"
    assert data["turn_id"] == "turn-1"


@pytest.mark.asyncio
async def test_sse_stream_starts_only_one_producer_per_turn() -> None:
    """Concurrent SSE subscribers for one turn must not start duplicate producers."""

    bus = RuntimeEventBus()
    runtime = _BlockingRuntime()
    first_stream = cast(
        AsyncGenerator[str, None],
        _sse_turn_events(
            runtime=runtime,  # type: ignore[arg-type]
            turn_id="turn-1",
            event_bus=bus,
        ),
    )
    second_stream = cast(
        AsyncGenerator[str, None],
        _sse_turn_events(
            runtime=runtime,  # type: ignore[arg-type]
            turn_id="turn-1",
            event_bus=bus,
        ),
    )
    first_frame = asyncio.ensure_future(first_stream.__anext__())
    second_frame = asyncio.ensure_future(second_stream.__anext__())

    await asyncio.wait_for(runtime.started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert runtime.run_count == 1

    runtime.release.set()
    frames = await asyncio.wait_for(asyncio.gather(first_frame, second_frame), timeout=1)
    await first_stream.aclose()
    await second_stream.aclose()

    assert all(frame.startswith("event: run_cancelled\n") for frame in frames)


@pytest.mark.asyncio
async def test_sse_stream_forwards_events_yielded_by_runtime() -> None:
    """SSE must honor the run_turn event stream even when runtime does not publish."""

    bus = RuntimeEventBus()
    stream = cast(
        AsyncGenerator[str, None],
        _sse_turn_events(
            runtime=_YieldingRuntime(),  # type: ignore[arg-type]
            turn_id="turn-1",
            event_bus=bus,
        ),
    )

    frame = await asyncio.wait_for(stream.__anext__(), timeout=1)
    await stream.aclose()

    assert frame.startswith("event: run_cancelled\n")
    data = json.loads(frame.split("data: ", 1)[1])
    assert data["event_type"] == "run_cancelled"
    assert data["turn_id"] == "turn-1"
