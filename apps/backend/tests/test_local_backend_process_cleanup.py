"""execute_terminal 的命令经 worker 执行模式运行：结束时清理它留下的进程，超时与停止先温和后强制，
后端被强杀时命令也被清理，退出码语义不变。"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.tools.tool_handler.terminal.local_backend import LocalExecutionBackend
from tests.terminal_test_support import requires_terminal_worker

pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="POSIX process groups and signals"),
    requires_terminal_worker,
]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _assert_gone(pids: list[int], what: str, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    alive = list(pids)
    while alive and time.monotonic() < deadline:
        alive = [pid for pid in alive if _alive(pid)]
        time.sleep(0.05)
    if alive:
        for pid in alive:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        pytest.fail(f"{what} {alive} survived")


def test_processes_left_behind_are_cleaned_without_holding_the_call(tmp_path: Path) -> None:
    """旧实现里 ``&`` / ``nohup`` 进程全部存活，握着输出管道的那个还让调用多等 5 秒。"""

    started = time.monotonic()
    result = LocalExecutionBackend().execute(
        "sleep 60 & a=$!; nohup sleep 60 >/dev/null 2>&1 & b=$!; echo \"PIDS $a $b\"",
        str(tmp_path),
        30,
    )

    assert result.exit_code == 0
    assert time.monotonic() - started < 3
    pids = [int(pid) for pid in result.output.split("PIDS", 1)[1].split()]
    _assert_gone(pids, "process left behind by the command")


def test_timeout_stops_the_command_gently_first(tmp_path: Path) -> None:
    """超时先发 HUP / TERM：命令有机会收尾（这里写下标记），宽限之后才 KILL。"""

    marker = tmp_path / "hup"
    result = LocalExecutionBackend().execute(
        f"trap 'echo got-hup > {marker}; exit 0' HUP; echo ready; while :; do sleep 0.05; done",
        str(tmp_path),
        0.5,
    )

    assert result.end == "timed_out" and result.exit_code is None
    assert "ready" in result.output
    assert marker.read_text().strip() == "got-hup"


def test_a_command_killed_by_a_signal_reports_the_signal(tmp_path: Path) -> None:
    result = LocalExecutionBackend().execute("kill -TERM $$", str(tmp_path), 10)

    assert result.end == "signaled"
    assert result.signal == signal.SIGTERM and result.exit_code is None


def test_a_command_killed_by_sighup_is_not_mistaken_for_a_stop(tmp_path: Path) -> None:
    """SIGHUP 的旧编码是 -1，与「被停止」的占位值相撞；现在结局与信号各自独立。"""
    result = LocalExecutionBackend().execute("kill -HUP $$", str(tmp_path), 10)

    assert result.end == "signaled" and result.signal == signal.SIGHUP


def test_stop_before_the_command_starts_never_starts_it(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    backend = LocalExecutionBackend()
    backend.stop()

    result = backend.execute(f"touch {marker}", str(tmp_path), 30)

    assert result.end == "cancelled"
    time.sleep(0.3)
    assert not marker.exists()


def test_a_stop_after_the_command_exited_keeps_its_exit(tmp_path: Path) -> None:
    """命令已自己结束、worker 还在清理它留下的进程时才到的停止，不把结局改写成取消。"""
    backend = LocalExecutionBackend()
    threading.Timer(0.3, backend.stop).start()

    result = backend.execute(
        "(trap '' HUP TERM; exec sleep 5) & echo done", str(tmp_path), 30
    )

    assert result.end == "exited" and result.exit_code == 0
    assert "done" in result.output


def test_a_missing_working_directory_is_a_start_failure(tmp_path: Path) -> None:
    result = LocalExecutionBackend().execute("echo hi", str(tmp_path / "missing"), 10)

    assert result.end == "start_failed"
    assert result.failure and result.exit_code is None


def test_command_is_cleaned_up_when_the_backend_process_group_is_killed(tmp_path: Path) -> None:
    """宿主强杀后端时对整组发 SIGKILL：worker 自成会话不被波及，读到 stdin EOF 后结束命令。

    命令里的进程忽略 HUP 并握着输出管道：worker 若跟着被杀，PTY 那种内核兜底这里没有，进程会
    成为孤儿。
    """

    job_file = tmp_path / "job.pid"
    script = f"""
import time
from app.tools.tool_handler.terminal.local_backend import LocalExecutionBackend
LocalExecutionBackend().execute(
    "trap '' HUP TERM; sleep 60 & echo $! > {job_file}; wait", {str(tmp_path)!r}, 120
)
time.sleep(60)
"""
    backend = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not (job_file.exists() and job_file.read_text().strip()):
            assert time.monotonic() < deadline, "job pid was not written"
            time.sleep(0.05)
        job_pid = int(job_file.read_text())
        assert _alive(job_pid)

        os.killpg(backend.pid, signal.SIGKILL)
        backend.wait(5)

        _assert_gone([job_pid], "command process after the backend was killed", timeout=5)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(backend.pid, signal.SIGKILL)
