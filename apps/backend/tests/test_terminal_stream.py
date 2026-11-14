"""交互通道 terminal-v2：asyncio 桥接订阅、背压与 WS 帧（用户输入 / resize）。"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from terminal_test_support import ReadyWorker, make_service, start

from app.config.constant import Constant
from app.tools.terminal_runtime.session.stream import (
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
    assert subscription._queue.empty()


def test_closed_loop_is_absorbed_at_subscription_boundary() -> None:
    loop = asyncio.new_event_loop()
    subscription = TerminalStreamSubscription(loop, "s")
    loop.close()
    subscription.publish({"type": "output"}, 1)  # 不抛出
    subscription.publish({"type": "output"}, 1)


@pytest.mark.asyncio
async def test_stream_attach_replays_then_streams_state_frames() -> None:
    buffer = TerminalStreamBuffer("s", 1024)
    buffer.append(b"a")
    attachment = buffer.attach(
        None, asyncio.get_running_loop(), status="running", rows=24, columns=80
    )
    assert attachment.attached["protocol"] == "terminal-v2"
    assert attachment.attached["next_seq"] == 2 and attachment.replay[0]["seq"] == 1
    buffer.publish("geometry", rows=30, columns=100)
    event = await asyncio.wait_for(attachment.subscription.get(), 1)
    assert event["type"] == "geometry" and event["rows"] == 30
    buffer.finish()
    assert await asyncio.wait_for(attachment.subscription.get(), 1) == {
        "protocol": "terminal-v2", "terminal_session_id": "s", "type": "exit",
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
async def test_websocket_input_resize_and_exit(monkeypatch, tmp_path: Path):
    from app.api.terminal_stream_api import terminal_session_stream

    service, workers = make_service(monkeypatch)
    terminal_session_id = start(service, tmp_path)
    workers[0].output(b"hello")
    ws = FakeWebSocket()
    ws.push({"type": "attach", "after_seq": None})
    task = asyncio.create_task(terminal_session_stream(ws, 1, terminal_session_id, service))
    await _settle(lambda: "output" in ws.types())
    attached = ws.sent[0]
    assert attached["type"] == "attached" and attached["protocol"] == "terminal-v2"
    assert attached["rows"] == 24 and "input_holder" not in attached

    ws.push({"type": "input", "data_base64": base64.b64encode(b"ls\r").decode()})
    await _settle(lambda: bool(workers[0].writes) and workers[0].writes[-1]["type"] == "raw")

    ws.push({"type": "resize", "rows": 30, "cols": 100})
    await _settle(lambda: workers[0].resizes == [(30, 100)])
    workers[0].screen(["$ "], size=(30, 100))
    await _settle(lambda: "geometry" in ws.types())

    # 交接等会话事实只经 snapshot 投影，不在 WS 上推送。
    assert "handoff" not in ws.types()

    workers[0].exit(0)
    await asyncio.wait_for(task, 2)
    assert ws.types()[-1] == "exit" and ws.closed_with == (1000, "")


@pytest.mark.asyncio
async def test_websocket_accepts_user_input_after_agent_prefill(monkeypatch, tmp_path: Path):
    from app.api.terminal_stream_api import terminal_session_stream

    service, workers = make_service(monkeypatch, worker_type=ReadyWorker)
    terminal_session_id = start(service, tmp_path)
    workers[0].screen(["$ "], cursor=(0, 2))
    workers[0].shell("shell_prompt", cwd="/w")
    ws = FakeWebSocket()
    ws.push({"type": "attach", "after_seq": None})
    task = asyncio.create_task(terminal_session_stream(ws, 1, terminal_session_id, service))
    await _settle(lambda: "attached" in ws.types())
    service.require(terminal_session_id, 1).prefill(
        "echo hi", run_id=1, tool_call_id="call-prefill"
    )
    await _settle(lambda: "input_mark" in ws.types())
    assert workers[0].writes == [{"type": "prefill", "text": "echo hi"}]
    # 填入不执行、也不占用输入权：用户随后按键照常送达。
    ws.push({"type": "input", "data_base64": base64.b64encode(b"q").decode()})
    await _settle(lambda: len(workers[0].writes) == 2)
    assert workers[0].writes[-1] == {"type": "raw", "data_base64": "cQ=="}
    assert "input_rejected" not in ws.types()
    ws.disconnect()
    await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_websocket_protocol_errors(monkeypatch, tmp_path: Path):
    """交还交接走 HTTP 决定入口，WS 不再接受 ``handoff_done``，与未知帧一样按协议错误关闭。"""
    from app.api.terminal_stream_api import terminal_session_stream

    service, _ = make_service(monkeypatch)
    terminal_session_id = start(service, tmp_path)
    ws = FakeWebSocket()
    ws.push({"type": "attach", "after_seq": None})
    ws.push({"type": "handoff_done"})
    await asyncio.wait_for(terminal_session_stream(ws, 1, terminal_session_id, service), 2)
    assert ws.types()[-1] == "protocol_error" and ws.closed_with is not None
    assert ws.closed_with[0] == 1008
    assert ws.sent[-1]["code"] == "TERMINAL_STREAM_PROTOCOL_ERROR"

    missing = FakeWebSocket()
    missing.push({"type": "attach", "after_seq": None})
    await asyncio.wait_for(terminal_session_stream(missing, 2, terminal_session_id, service), 2)
    assert missing.sent[-1]["code"] == "TERMINAL_SESSION_OWNERSHIP_ERROR"
    assert missing.sent[-1]["message"] == "terminal session is not owned by this session"

    unknown = FakeWebSocket()
    unknown.push({"type": "attach", "after_seq": None})
    await asyncio.wait_for(terminal_session_stream(unknown, 1, "term_missing", service), 2)
    assert unknown.sent[-1]["code"] == "TERMINAL_SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_websocket_backpressure_turns_into_resync(monkeypatch, tmp_path: Path):
    from app.api.terminal_stream_api import terminal_session_stream

    monkeypatch.setattr(Constant.Terminal, "MAX_STREAM_QUEUE_FRAMES", 3)
    service, workers = make_service(monkeypatch)
    terminal_session_id = start(service, tmp_path)
    ws = FakeWebSocket()
    ws.push({"type": "attach", "after_seq": None})
    original = ws.send_json
    gate = asyncio.Event()

    async def slow_send(payload: dict[str, Any]) -> None:
        if payload["type"] == "output":
            await gate.wait()
        await original(payload)

    ws.send_json = slow_send  # type: ignore[method-assign]
    task = asyncio.create_task(terminal_session_stream(ws, 1, terminal_session_id, service))
    await _settle(lambda: "attached" in ws.types())
    for _ in range(10):
        workers[0].output(b"z")
    gate.set()
    await asyncio.wait_for(task, 2)
    assert "resync_required" in ws.types()
    assert ws.closed_with is not None and ws.closed_with[0] == 1013


@pytest.mark.asyncio
async def test_worker_write_backpressure_reaches_the_panel(monkeypatch, tmp_path: Path):
    """目的：前台程序不读输入、worker 写入队列满时面板能看到；潜在缺陷：只写日志，用户以为粘贴已送达。"""
    from app.api.terminal_stream_api import terminal_session_stream

    service, workers = make_service(monkeypatch)
    terminal_session_id = start(service, tmp_path)
    ws = FakeWebSocket()
    ws.push({"type": "attach", "after_seq": None})
    task = asyncio.create_task(terminal_session_stream(ws, 1, terminal_session_id, service))
    await _settle(lambda: "attached" in ws.types())

    workers[0].emit({"type": "error", "code": "input_backpressure"})

    await _settle(lambda: "input_rejected" in ws.types())
    rejected = next(frame for frame in ws.sent if frame["type"] == "input_rejected")
    assert rejected["reason"] == "backpressure"
    assert service.require(terminal_session_id, 1).snapshot().status == "running"
    ws.disconnect()
    await asyncio.wait_for(task, 2)
