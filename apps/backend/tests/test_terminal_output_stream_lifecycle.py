"""``TerminalOutputStream`` 的关闭语义：``finish`` 不阻塞，事件循环停摆时不能挂住工具线程。"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from app.core.events import TerminalOutputDeltaData
from app.core.tool.schemas import ToolExecutionContext
from app.tools.tool_handler.terminal.output_stream import TerminalOutputStream


def _context(loop: asyncio.AbstractEventLoop, sink) -> ToolExecutionContext:
    return ToolExecutionContext(
        step_id="step-1",
        task_id=7,
        workspace_id=1,
        workspace_root=Path("."),
        run_id=9,
        event_loop=loop,
        event_sink=sink,
        tool_call_id="call-1",
    )


def _stopped_loop() -> asyncio.AbstractEventLoop:
    """起一个真实事件循环再停掉（不 close）：此后排队的回调不会再执行。"""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    ready = threading.Event()
    loop.call_soon_threadsafe(ready.set)
    assert ready.wait(2), "事件循环没有启动"
    loop.call_soon_threadsafe(loop.stop)
    thread.join(2)
    assert not thread.is_alive(), "事件循环没有停下"
    return loop


def test_finish_returns_while_event_loop_is_stopped() -> None:
    """回归：``finish`` 曾阻塞等待 drain，而停掉的循环永不执行该回调，工具线程永久挂死。"""

    loop = _stopped_loop()
    stream = TerminalOutputStream.open(_context(loop, lambda _event: None))
    assert stream is not None
    stream.emit("pending-when-loop-stopped")

    done = threading.Event()
    threading.Thread(target=lambda: (stream.finish(), done.set()), daemon=True).start()

    assert done.wait(2), "事件循环停摆时 finish 必须立即返回"


async def test_finish_is_idempotent_and_late_output_is_dropped() -> None:
    """重复 ``finish`` 无副作用；关闭后到达的文本静默丢弃（后台转走后就是这样）。"""

    loop = asyncio.get_running_loop()
    events: list[object] = []
    stream = TerminalOutputStream.open(_context(loop, events.append))
    assert stream is not None

    stream.finish()
    stream.finish()
    stream.emit("late-output")
    await asyncio.sleep(0.1)

    assert events == []


async def test_finish_still_publishes_already_received_text() -> None:
    """不阻塞不等于丢帧：已接收文本仍会在事件循环上排空并投递。"""

    loop = asyncio.get_running_loop()
    events: list = []
    stream = TerminalOutputStream.open(_context(loop, events.append))
    assert stream is not None
    stream.emit("before-finish")

    await asyncio.to_thread(stream.finish)

    chunks = [
        event.data.text for event in events if isinstance(event.data, TerminalOutputDeltaData)
    ]
    assert "".join(chunks) == "before-finish"
