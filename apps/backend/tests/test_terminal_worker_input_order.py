"""Terminal Worker 客户端的输入有序性：write 与 resize 走同一条队列，不越过排队中的输入。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.tools.terminal_runtime import worker_client as worker_module
from app.tools.terminal_runtime.worker_client import ProcessTerminalWorker


def _decode(frame: bytes) -> dict[str, object]:
    return json.loads(frame[4:].decode("utf-8"))


def test_write_and_resize_share_the_ordered_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(worker_module.Constant.Terminal.WORKER_ENV, "terminal-worker")
    worker = ProcessTerminalWorker("order-test")
    worker._process = SimpleNamespace(poll=lambda: None, pid=1)  # type: ignore[assignment]
    worker._handshake_valid = True
    direct: list[dict[str, object]] = []
    monkeypatch.setattr(worker, "_send", direct.append)

    worker.write({"type": "prefill", "text": "make"})
    worker.resize(30, 100)

    frames = [_decode(worker._input_queue.get_nowait()) for _ in range(2)]
    assert [frame["type"] for frame in frames] == ["write", "resize"]
    assert frames[0]["input"] == {"type": "prefill", "text": "make"}
    assert frames[1] == {"type": "resize", "rows": 30, "cols": 100}
    assert direct == []
