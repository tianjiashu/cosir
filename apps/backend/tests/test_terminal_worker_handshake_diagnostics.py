"""Terminal Worker 握手失败的错误分类与可排查日志测试。

覆盖：握手身份不匹配（确定性）判为不可重试、握手超时仍可重试、两类失败各自的 WARNING 事件与字段
对照，以及经 ``service_error_observation`` 投影给模型时 ``retryable``/``reason`` 的变化。
"""

from __future__ import annotations

import io
import json
import logging
import struct
import subprocess
import threading
from types import SimpleNamespace

import pytest

from app.tools.tool_handler.terminal_session.common import service_error_observation
from app.tools.terminal_runtime import process_terminal_worker as worker_module
from app.tools.terminal_runtime.errors import (
    TerminalWorkerHandshakeError,
    TerminalWorkerProtocolError,
    TerminalWorkerUnavailableError,
)
from app.tools.terminal_runtime.process_terminal_worker import ProcessTerminalWorker
from app.tools.terminal_runtime.shell_resolver import ShellSpec

_FAKE_WORKER_PID = 4242


@pytest.fixture(autouse=True)
def _configure_worker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # 正常用例需 worker 可执行已配置；占位值被 _patch_worker_process 的 mock 覆盖。
    monkeypatch.setenv(worker_module.Constant.Terminal.WORKER_ENV, "terminal-worker")


class _SilentStream:
    """只阻塞、不产出数据的 stdout 替身：模拟 worker 存活但不回应握手。

    参数:
        无。

    返回:
        ``_SilentStream`` 实例。

    异常:
        无。

    副作用:
        ``read`` 会在内部 Event 上永久等待，故只能被 daemon 读取线程使用。
    """

    def __init__(self) -> None:
        self._gate = threading.Event()

    def read(self, size: int = -1) -> bytes:
        del size
        # 永不置位：让 reader 线程停在这里，等价于「worker 在超时内没有发出握手」。
        self._gate.wait()
        return b""


class _FakeProcess:
    """``subprocess.Popen`` 的最小替身：提供握手字节流与 ``close()`` 需要的接口。

    参数:
        handshake_frames: 预先写入 stdout 的帧字节；``None`` 表示 worker 不回应握手（stdout
            永久阻塞），空字节串表示 stdout 立即 EOF。

    返回:
        ``_FakeProcess`` 实例。

    异常:
        无。

    副作用:
        无（内存 io 对象；``None`` 分支的阻塞只发生在一个 daemon 线程内）。
    """

    def __init__(self, handshake_frames: bytes | None = None) -> None:
        self.pid = _FAKE_WORKER_PID
        self.stdin = io.BytesIO()
        self.stdout = _SilentStream() if handshake_frames is None else io.BytesIO(handshake_frames)
        self.stderr = io.BytesIO(b"")
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self._returncode = 0
        return 0

    def terminate(self) -> None:
        self._returncode = -15

    def kill(self) -> None:
        self._returncode = -9


def _frame(event: dict[str, object]) -> bytes:
    """把事件编码为 worker 的长度前缀 JSON 帧。

    参数:
        event: 事件字典。

    返回:
        ``4 字节大端长度 + UTF-8 JSON`` 的帧字节。

    异常:
        无。

    副作用:
        无。
    """

    payload = json.dumps(event).encode("utf-8")
    return struct.pack(">I", len(payload)) + payload


def _spec() -> ShellSpec:
    """构造最小 shell 规格（Popen 已被替身替换，不会真正执行）。"""

    return ShellSpec("cmd", "cmd.exe", ("cmd.exe",))


def _patch_worker_process(monkeypatch: pytest.MonkeyPatch, process: _FakeProcess) -> None:
    """把 ``process_terminal_worker.py`` 使用的 subprocess 替换为受控替身。

    参数:
        monkeypatch: pytest 补丁夹具。
        process: 期望 ``Popen`` 返回的假进程。

    返回:
        无。

    异常:
        无。

    副作用:
        仅替换 ``process_terminal_worker`` 模块内的 ``subprocess`` 引用，
        不改动全局 ``subprocess``，避免影响其它测试。
    """

    monkeypatch.setattr(
        worker_module,
        "subprocess",
        SimpleNamespace(
            Popen=lambda *args, **kwargs: process,
            PIPE=subprocess.PIPE,
            TimeoutExpired=subprocess.TimeoutExpired,
            CREATE_NO_WINDOW=0x08000000,
        ),
    )


def _handshake_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    """取出唯一一条握手失败日志记录。

    参数:
        caplog: pytest 日志捕获夹具。

    返回:
        ``terminal_worker_handshake_failed`` 对应的日志记录。

    异常:
        StopIteration: 未产生该事件时（用例期望它必然产生）。

    副作用:
        无。
    """

    return next(
        record
        for record in caplog.records
        if record.getMessage() == "terminal_worker_handshake_failed"
    )


def test_handshake_error_is_non_retryable_worker_unavailable_subclass() -> None:
    """握手身份不匹配不可重试，同时保留父类身份以免既有捕获点漏接。"""

    assert issubclass(TerminalWorkerHandshakeError, TerminalWorkerUnavailableError)
    assert TerminalWorkerHandshakeError.code == "TERMINAL_WORKER_HANDSHAKE_REJECTED"
    assert TerminalWorkerHandshakeError.retryable is False
    assert TerminalWorkerUnavailableError.retryable is True


def test_service_error_observation_marks_rejected_handshake_as_not_retryable() -> None:
    """投影给模型的握手错误必须不可重试，并给「先修再试」而非「稍后重试」。"""

    observation = service_error_observation(
        "terminal_start",
        TerminalWorkerHandshakeError("handshake rejected"),
    )

    assert observation.status == "error"
    assert "hint: do not retry this tool call." in observation.content
    assert observation.display_data["status_hint"] == "终端失败"
    assert "TERMINAL_WORKER_HANDSHAKE_REJECTED" in observation.content
    assert "Fix the session or arguments before retrying." in observation.content


def test_rejected_handshake_raises_handshake_error_and_logs_field_comparison(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """收到身份不匹配的握手时抛不可重试错误，并记录期望/实际字段对照。"""

    caplog.set_level(logging.WARNING)
    stale_handshake = _frame(
        {
            "type": "handshake",
            "instance_id": "stale-instance",
            "pid": _FAKE_WORKER_PID,
            "pty_kind": "conpty",
            "rows": 24,
            "columns": 80,
            "capabilities": [],
        }
    )
    _patch_worker_process(monkeypatch, _FakeProcess(stale_handshake))
    worker = ProcessTerminalWorker("expected-instance")

    with pytest.raises(TerminalWorkerHandshakeError) as raised:
        worker.start(_spec(), ".", lambda event: None)

    message = str(raised.value)
    assert "expected-instance" in message
    assert "stale-instance" in message
    assert "verify that the configured worker executable belongs to this application" in message

    record = _handshake_record(caplog)
    data = record.data  # type: ignore[attr-defined]
    assert data["reason"] == "rejected"
    assert data["received_pid"] == _FAKE_WORKER_PID
    assert data["received_rows"] == 24
    assert data["received_columns"] == 80


def test_handshake_timeout_stays_retryable_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """握手超时属可能瞬时的失败：保持可重试的 Unavailable，并在日志里标记 reason=timeout。"""

    caplog.set_level(logging.WARNING)
    _patch_worker_process(monkeypatch, _FakeProcess())
    monkeypatch.setattr(worker_module, "HANDSHAKE_TIMEOUT_SECONDS", 0.05)
    worker = ProcessTerminalWorker("timeout-instance")

    with pytest.raises(TerminalWorkerUnavailableError) as raised:
        worker.start(_spec(), ".", lambda event: None)

    assert not isinstance(raised.value, TerminalWorkerProtocolError)
    assert "timed out" in str(raised.value)

    record = _handshake_record(caplog)
    data = record.data  # type: ignore[attr-defined]
    assert data["reason"] == "timeout"
    assert data["timeout_seconds"] == 0.05
    assert data["received_instance_id"] is None


def test_not_configured_worker_logs_before_raising(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """未配置 worker 可执行文件时要留下可定位日志，而不是只有一条异常。"""

    caplog.set_level(logging.WARNING)
    monkeypatch.delenv(worker_module.Constant.Terminal.WORKER_ENV, raising=False)

    with pytest.raises(TerminalWorkerUnavailableError):
        ProcessTerminalWorker("unconfigured-instance")

    assert any(record.getMessage() == "terminal_worker_not_configured" for record in caplog.records)


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_spawn_flags_only_require_windows_constant_on_windows(monkeypatch, platform):
    captured = {}

    def spawn(*args, **kwargs):
        captured.update(kwargs)
        raise OSError("stop before process creation")

    process_api = SimpleNamespace(Popen=spawn, PIPE=subprocess.PIPE)
    if platform == "win32":
        process_api.CREATE_NO_WINDOW = 0x08000000
    monkeypatch.setattr(worker_module, "subprocess", process_api)
    monkeypatch.setattr(worker_module, "sys", SimpleNamespace(platform=platform))
    worker = ProcessTerminalWorker("platform-flags")
    with pytest.raises(TerminalWorkerUnavailableError, match="failed to start"):
        worker.start(_spec(), ".", lambda event: None)
    assert captured["creationflags"] == (0x08000000 if platform == "win32" else 0)
