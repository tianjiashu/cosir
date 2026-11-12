"""执行后端的停止收尾：超时与取消同一条路，任何停止请求之后都只限时等 worker 报回。

用不报回结局的 worker 替身：真实 worker 只有在命令收到 KILL 也不退出时才会这样，这里确定性地复现。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.tools.terminal_runtime.command import local_backend
from app.tools.terminal_runtime.command.command_output import CommandOutput
from app.tools.terminal_runtime.command.local_backend import LocalExecutionBackend


class _SilentWorker:
    """握手成功、之后什么也不报回的 worker。"""

    def __init__(self, _name: str) -> None:
        self.shutdowns = 0
        self.closed = False

    def start_exec(self, *_args: object, **_kwargs: object) -> None:
        return None

    def shutdown(self) -> None:
        self.shutdowns += 1

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def silent_worker(monkeypatch: pytest.MonkeyPatch) -> list[_SilentWorker]:
    workers: list[_SilentWorker] = []

    def make(name: str) -> _SilentWorker:
        worker = _SilentWorker(name)
        workers.append(worker)
        return worker

    monkeypatch.setattr(local_backend, "ProcessTerminalWorker", make)
    monkeypatch.setattr(local_backend, "_SHUTDOWN_WAIT_SECONDS", 0.2)
    return workers


def _run(backend: LocalExecutionBackend, tmp_path: Path, timeout: float | None):
    output = CommandOutput(workspace_root=tmp_path, model_budget_chars=1000)
    results = []
    thread = threading.Thread(
        target=lambda: results.append(backend.execute("sleep 100", str(tmp_path), timeout, output)),
    )
    thread.start()
    return thread, results


def test_cancelling_an_unlimited_command_returns_within_the_shutdown_deadline(
    tmp_path: Path, silent_worker: list[_SilentWorker],
) -> None:
    backend = LocalExecutionBackend()
    thread, results = _run(backend, tmp_path, None)
    while not silent_worker:
        threading.Event().wait(0.01)
    backend.stop()
    thread.join(2)
    assert not thread.is_alive()
    [result] = results
    assert result.end == "cancelled"
    assert silent_worker[0].shutdowns == 1 and silent_worker[0].closed


def test_cancelling_does_not_wait_out_the_remaining_command_timeout(
    tmp_path: Path, silent_worker: list[_SilentWorker],
) -> None:
    backend = LocalExecutionBackend()
    thread, results = _run(backend, tmp_path, 60)
    while not silent_worker:
        threading.Event().wait(0.01)
    backend.stop()
    thread.join(2)
    assert not thread.is_alive() and results[0].end == "cancelled"


def test_timeout_still_stops_and_reports_timed_out(
    tmp_path: Path, silent_worker: list[_SilentWorker],
) -> None:
    backend = LocalExecutionBackend()
    thread, results = _run(backend, tmp_path, 0.1)
    thread.join(2)
    assert not thread.is_alive() and results[0].end == "timed_out"
