"""Terminal Worker 握手失败的错误分类与可排查日志测试。

覆盖：协议不兼容（确定性）判为不可重试、握手超时仍可重试、两类失败各自的 WARNING 事件与字段
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

from app.core.tools.tool_handler.terminal_session.common import service_error_observation
from app.service.terminal import worker as worker_module
from app.service.terminal.errors import (
    TerminalWorkerProtocolError,
    TerminalWorkerUnavailableError,
)
from app.service.terminal.shell_resolver import ShellSpec
from app.service.terminal.worker import ProcessTerminalWorker

_FAKE_WORKER_PID = 4242


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
    """把 ``worker.py`` 使用的 subprocess 替换为受控替身。

    参数:
        monkeypatch: pytest 补丁夹具。
        process: 期望 ``Popen`` 返回的假进程。

    返回:
        无。

    异常:
        无。

    副作用:
        仅替换 ``app.service.terminal.worker`` 模块内的 ``subprocess`` 引用，
        不改动全局 ``subprocess``，避免影响其它测试。
    """

    monkeypatch.setattr(
        worker_module,
        "subprocess",
        SimpleNamespace(
            Popen=lambda *args, **kwargs: process,
            PIPE=subprocess.PIPE,
            TimeoutExpired=subprocess.TimeoutExpired,
            CREATE_NO_WINDOW=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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


def test_protocol_error_is_non_retryable_worker_unavailable_subclass() -> None:
    """协议不兼容必须是「不可重试」，同时保留父类身份以免既有捕获点漏接。"""

    assert issubclass(TerminalWorkerProtocolError, TerminalWorkerUnavailableError)
    assert TerminalWorkerProtocolError.code == "TERMINAL_WORKER_PROTOCOL_MISMATCH"
    assert TerminalWorkerProtocolError.retryable is False
    assert TerminalWorkerUnavailableError.retryable is True


def test_service_error_observation_marks_protocol_mismatch_as_not_retryable() -> None:
    """投影给模型的协议不兼容错误必须是 retryable=False，并给「先修再试」而非「稍后重试」。"""

    observation = service_error_observation(
        "terminal_start",
        "shell",
        TerminalWorkerProtocolError("handshake rejected"),
    )

    assert observation.status == "error"
    assert observation.retryable is False
    assert observation.display_data["status_hint"] == "终端失败"
    assert "TERMINAL_WORKER_PROTOCOL_MISMATCH" in (observation.reason or "")
    assert "Fix the session or arguments before retrying." in (observation.reason or "")


def test_rejected_handshake_raises_protocol_error_and_logs_field_comparison(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """收到旧协议握手时要抛不可重试的协议错误，并在日志里给出期望/实际字段对照。"""

    caplog.set_level(logging.WARNING)
    stale_handshake = _frame(
        {
            "type": "handshake",
            "protocol": "terminal-worker-v1",
            "instance_id": "stale-instance",
            "pid": _FAKE_WORKER_PID,
            "pty_kind": "conpty",
            "capabilities": [],
        }
    )
    _patch_worker_process(monkeypatch, _FakeProcess(stale_handshake))
    worker = ProcessTerminalWorker("terminal-worker.exe", "expected-instance")

    with pytest.raises(TerminalWorkerProtocolError) as raised:
        worker.start(_spec(), ".", lambda event: None)

    message = str(raised.value)
    assert "expected-instance" in message
    assert "stale-instance" in message
    assert "terminal-worker-v1" in message
    assert worker_module.Constant.Terminal.WORKER_PROTOCOL in message

    record = _handshake_record(caplog)
    data = record.data  # type: ignore[attr-defined]
    assert data["reason"] == "rejected"
    assert data["received_protocol"] == "terminal-worker-v1"
    assert data["expected_protocol"] == worker_module.Constant.Terminal.WORKER_PROTOCOL
    assert data["received_pid"] == _FAKE_WORKER_PID


def test_handshake_timeout_stays_retryable_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """握手超时属可能瞬时的失败：保持可重试的 Unavailable，并在日志里标记 reason=timeout。"""

    caplog.set_level(logging.WARNING)
    _patch_worker_process(monkeypatch, _FakeProcess())
    monkeypatch.setattr(worker_module, "HANDSHAKE_TIMEOUT_SECONDS", 0.05)
    worker = ProcessTerminalWorker("terminal-worker.exe", "timeout-instance")

    with pytest.raises(TerminalWorkerUnavailableError) as raised:
        worker.start(_spec(), ".", lambda event: None)

    assert not isinstance(raised.value, TerminalWorkerProtocolError)
    assert "timed out" in str(raised.value)

    record = _handshake_record(caplog)
    data = record.data  # type: ignore[attr-defined]
    assert data["reason"] == "timeout"
    assert data["timeout_seconds"] == 0.05
    assert data["received_protocol"] is None


def test_not_configured_worker_logs_before_raising(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """未配置 worker 可执行文件时要留下可定位日志，而不是只有一条异常。"""

    caplog.set_level(logging.WARNING)
    monkeypatch.delenv(worker_module.Constant.Terminal.WORKER_ENV, raising=False)
    factory = worker_module.ProcessTerminalWorkerFactory()

    with pytest.raises(TerminalWorkerUnavailableError):
        factory.create("unconfigured-instance")

    assert any(record.getMessage() == "terminal_worker_not_configured" for record in caplog.records)
