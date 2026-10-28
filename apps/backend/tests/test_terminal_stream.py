"""交互通道 terminal-v2：asyncio 桥接订阅、背压与 WS 帧（输入 / resize / 租约 / 交接）。"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from terminal_test_support import make_service, start

from app.config.constant import Constant
from app.tools.terminal_runtime.terminal_stream import (
    OVERFLOW_EVENT_TYPE,
    TerminalStreamBuffer,
    TerminalStreamSubscription,
)


@pytest.mark.asyncio
async def test_subscription_bridges_publishes_from_other_threads() -> None:
    subscription = TerminalStreamSubscription(asyncio.get_running_loop(), "s")
    thread = threading.Thread(target=lambda: subscription.publish({"type": "output"}, 3))
    thread.start()
    thread.join()
    assert await asyncio.wait_for(subscription.get(), 1) == {"type": "output"}


@pytest.mark.asyncio
async def test_subscription_overflow_sends_single_sentinel(monkeypatch) -> None:
    monkeypatch.setattr(Constant.Terminal, "MAX_STREAM_QUEUE_FRAMES", 2)
    subscription = TerminalStreamSubscription(asyncio.get_running_loop(), "s")
    for index in range(5):
        subscription.publish({"type": "output", "seq": index}, 1)
    events = [await asyncio.wait_for(subscription.get(), 1) for _ in range(3)]
    assert events[-1] == {"type": OVERFLOW_EVENT_TYPE}
    assert subscription._queue.empty()  # noqa: SLF001 - 溢出后不再投递


def test_closed_loop_is_absorbed_at_subscription_boundary() -> None:
    loop = asyncio.new_event_loop()
    subscription = TerminalStreamSubscription(loop, "s")
    loop.close()
    subscription.publish({"type": "output"}, 1)  # 不抛出
    subscription.publish({"type": "output"}, 1)


@pytest.mark.asyncio
async def test_stream_attach_replays_then_streams_state_frames() -> None:
    buffer = TerminalStreamBuffer("s", "g", 1024)
    buffer.append("YQ==", 1)
    attachment = buffer.attach(
        None, asyncio.get_running_loop(), status="running", state={"rows": 24, "columns": 80}
    )
    assert attachment.attached["protocol"] == "terminal-v2"
    assert attachment.attached["next_seq"] == 2 and attachment.replay[0]["seq"] == 1
    buffer.publish("geometry", rows=30, columns=100)
    event = await asyncio.wait_for(attachment.subscription.get(), 1)
    assert event["type"] == "geometry" and event["rows"] == 30
    buffer.finish()
    assert await asyncio.wait_for(attachment.subscription.get(), 1) == {
        **{key: attachment.attached[key] for key in ("protocol", "session_id", "generation")},
        "type": "exit",
    }


class FakeWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed_with: tuple[int, str] | None = None

    def push(self, frame: dict[str, Any]) -> None:
        self.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(frame)})

    def disconnect(self) -> None:
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})

    async def accept(self) -> None:
        return None

    async def receive(self) -> dict[str, Any]:
        return await self.incoming.get()

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)
        self.disconnect()

    def types(self) -> list[str]:
        return [frame["type"] for frame in self.sent]


async def _settle(predicate: Any, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_websocket_input_resize_release_and_exit(monkeypatch, tmp_path: Path):
    from app.api.terminal_stream_api import terminal_session_stream

    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path, created_by="user")
    workers[0].output(b"hello")
    ws = FakeWebSocket()
    ws.push({"type": "attach", "after_seq": None})
    task = asyncio.create_task(terminal_session_stream(ws, 1, session_id, service))
    await _settle(lambda: "output" in ws.types())
    attached = ws.sent[0]
    assert attached["type"] == "attached" and attached["protocol"] == "terminal-v2"
    assert attached["rows"] == 24 and "input_holder" not in attached
    holder = lambda: service.project_task_sessions(1)[0]["inputHolder"]  # noqa: E731

    # 租约只经 snapshot 投影，不在 WS 上推送。
    ws.push({"type": "input", "data_base64": base64.b64encode(b"ls\r").decode()})
    await _settle(lambda: holder() == "user")
    assert workers[0].writes[-1]["type"] == "raw"

    ws.push({"type": "resize", "rows": 30, "cols": 100})
    await _settle(lambda: workers[0].resizes == [(30, 100)])
    workers[0].screen(["$ "], size=(30, 100))
    await _settle(lambda: "geometry" in ws.types())

    ws.push({"type": "release_input"})
    await _settle(lambda: holder() is None)
    assert not {"lease", "handoff"} & set(ws.types())

    workers[0].exit(0)
    await asyncio.wait_for(task, 2)
    assert ws.types()[-1] == "exit" and ws.closed_with == (1000, "")


@pytest.mark.asyncio
async def test_websocket_rejects_input_while_agent_holds(monkeypatch, tmp_path: Path):
    from app.api.terminal_stream_api import terminal_session_stream
    from app.tools.terminal_runtime.terminal_input import TerminalWait

    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    ws = FakeWebSocket()
    ws.push({"type": "attach", "after_seq": None})
    task = asyncio.create_task(terminal_session_stream(ws, 1, session_id, service))
    await _settle(lambda: "attached" in ws.types())
    writer = threading.Thread(target=lambda: service.write(
        session_id, task_id=1, operation_id="op", input={"type": "line", "text": "sleep"},
        wait=TerminalWait(until="elapsed", timeout_ms=500), run_id=1, is_cancelled=lambda: False))
    writer.start()
    await _settle(lambda: "input_mark" in ws.types())
    ws.push({"type": "input", "data_base64": base64.b64encode(b"q").decode()})
    await _settle(lambda: "input_rejected" in ws.types())
    assert ws.sent[-1]["reason"] == "agent_input"
    writer.join(2)
    ws.disconnect()
    await asyncio.wait_for(task, 2)
    assert workers[0].writes == [{"type": "line", "text": "sleep"}]


@pytest.mark.asyncio
async def test_websocket_handoff_done_and_protocol_errors(monkeypatch, tmp_path: Path):
    from app.api.terminal_stream_api import terminal_session_stream

    service, _ = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    called: list[str] = []
    monkeypatch.setattr(service, "complete_handoff",
                        lambda sid, *, task_id: called.append(sid))
    ws = FakeWebSocket()
    ws.push({"type": "attach", "after_seq": None})
    ws.push({"type": "handoff_done"})
    ws.push({"type": "write", "data": "x"})
    await asyncio.wait_for(terminal_session_stream(ws, 1, session_id, service), 2)
    assert called == [session_id]
    assert ws.types()[-1] == "protocol_error" and ws.closed_with is not None
    assert ws.closed_with[0] == 1008

    missing = FakeWebSocket()
    missing.push({"type": "attach", "after_seq": None})
    await asyncio.wait_for(terminal_session_stream(missing, 2, session_id, service), 2)
    assert missing.sent[-1]["message"] == "terminal session is not owned by this task"


@pytest.mark.asyncio
async def test_websocket_backpressure_turns_into_resync(monkeypatch, tmp_path: Path):
    from app.api.terminal_stream_api import terminal_session_stream

    monkeypatch.setattr(Constant.Terminal, "MAX_STREAM_QUEUE_FRAMES", 3)
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    ws = FakeWebSocket()
    ws.push({"type": "attach", "after_seq": None})
    original = ws.send_json
    gate = asyncio.Event()

    async def slow_send(payload: dict[str, Any]) -> None:
        if payload["type"] == "output":
            await gate.wait()
        await original(payload)

    ws.send_json = slow_send  # type: ignore[method-assign]
    task = asyncio.create_task(terminal_session_stream(ws, 1, session_id, service))
    await _settle(lambda: "attached" in ws.types())
    for _ in range(10):
        workers[0].output(b"z")
    gate.set()
    await asyncio.wait_for(task, 2)
    assert "resync_required" in ws.types()
    assert ws.closed_with is not None and ws.closed_with[0] == 1013
