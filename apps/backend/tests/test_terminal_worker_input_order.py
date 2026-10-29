"""Terminal Worker 客户端的输入有序性：signal 与 write / resize 走同一条队列，不越过排队中的输入。"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from app.tools.terminal_runtime import worker_client as worker_module
from app.tools.terminal_runtime.worker_client import ProcessTerminalWorker
from terminal_test_support import wait_until


def _decode(frame: bytes) -> dict[str, object]:
    return json.loads(frame[4:].decode("utf-8"))


def test_signal_is_queued_behind_pending_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(worker_module.Constant.Terminal.WORKER_ENV, "terminal-worker")
    worker = ProcessTerminalWorker("order-test")
    # 已握手、写线程尚未取走队列：模拟输入还排着时 Agent 发出 signal。
    worker._process = SimpleNamespace(poll=lambda: None, pid=1)  # type: ignore[assignment]
    worker._handshake_valid = True
    direct: list[dict[str, object]] = []
    monkeypatch.setattr(worker, "_send", direct.append)

    worker.write({"type": "line", "text": "make"})
    errors: list[BaseException] = []

    def send_signal() -> None:
        try:
            worker.signal("interrupt")
        except BaseException as exc:  # noqa: BLE001 - 带回主线程断言
            errors.append(exc)

    sender = threading.Thread(target=send_signal)
    sender.start()
    wait_until(lambda: worker._input_queue.qsize() == 2)
    frames = [_decode(worker._input_queue.get_nowait()) for _ in range(2)]
    assert [frame["type"] for frame in frames] == ["write", "signal"]
    assert direct == []
    # 读线程收到回执后 signal 返回。
    request_id = frames[1]["request_id"]
    with worker._signal_lock:
        pending = worker._pending_signals[str(request_id)]
        pending.result = {"type": "signal_result", "status": "applied"}
        pending.event.set()
    sender.join(2)
    assert not sender.is_alive() and errors == []
