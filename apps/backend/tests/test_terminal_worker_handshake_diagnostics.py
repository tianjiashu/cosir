"""Terminal Worker 握手失败与读线程结束的错误分类、可排查日志测试。

覆盖：握手身份不匹配（确定性）判为不可重试、握手超时与握手前断开仍可重试、各类失败的 WARNING 事件
与字段对照、经 ``service_error_observation`` 投影给模型时重试建议文本的变化，以及握手后读线程
意外结束（断开、事件处理出错）补发致命错误而非伪装成 shell 退出。
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

from app.tools.terminal_runtime import worker_client as worker_module
from app.tools.terminal_runtime.errors import (
    TerminalWorkerHandshakeError,
    TerminalWorkerProtocolError,
    TerminalWorkerUnavailableError,
)
from app.tools.terminal_runtime.worker_client import ProcessTerminalWorker
from app.tools.terminal_runtime.shell_resolver import ShellSpec
from app.tools.tool_handler.terminal_session.common import service_error_observation

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
    """把 ``worker_client.py`` 使用的 subprocess 替换为受控替身。

    参数:
        monkeypatch: pytest 补丁夹具。
        process: 期望 ``Popen`` 返回的假进程。

    返回:
        无。

    异常:
        无。

    副作用:
        仅替换 ``worker_client`` 模块内的 ``subprocess`` 引用，
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


def _valid_handshake(instance_id: str) -> bytes:
    return _frame(
        {
            "type": "handshake",
            "instance_id": instance_id,
            "pid": _FAKE_WORKER_PID,
            "pty_kind": "unix_pty",
            "rows": 24,
            "columns": 80,
            "capabilities": [],
        }
    )


def test_disconnect_before_handshake_is_retryable_and_emits_nothing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """握手前 stdout 即 EOF（worker 崩溃）：不是「握手不符」，保持可重试且不上报任何事件。"""

    caplog.set_level(logging.WARNING)
    _patch_worker_process(monkeypatch, _FakeProcess(b""))
    events: list[dict[str, object]] = []
    worker = ProcessTerminalWorker("crash-instance")

    with pytest.raises(TerminalWorkerUnavailableError) as raised:
        worker.start(_spec(), ".", lambda event: events.append(dict(event)))

    assert not isinstance(raised.value, TerminalWorkerHandshakeError)
    assert "before handshake" in str(raised.value)
    assert _handshake_record(caplog).data["reason"] == "disconnected"  # type: ignore[attr-defined]
    assert events == []


def _start_and_collect(
    monkeypatch: pytest.MonkeyPatch, frames: bytes, on_event=None
) -> tuple[ProcessTerminalWorker, list[dict[str, object]], threading.Event]:
    """以「合法握手 + ``frames`` + EOF」启动 worker，收集事件直到收到补发的致命错误。"""

    _patch_worker_process(monkeypatch, _FakeProcess(_valid_handshake("live") + frames))
    events: list[dict[str, object]] = []
    disconnected = threading.Event()

    def callback(event):
        if event.get("type") == "handshake":
            return
        events.append(dict(event))
        if event.get("code") == "WORKER_DISCONNECTED":
            disconnected.set()
        elif on_event is not None:
            on_event(event)

    worker = ProcessTerminalWorker("live")
    worker.start(_spec(), ".", callback)
    return worker, events, disconnected


def test_reader_end_after_handshake_reports_fatal_error_not_exit(monkeypatch) -> None:
    """worker 未发 exit 就断开：补发 WORKER_DISCONNECTED，不能伪装成 shell 正常退出。"""

    worker, events, disconnected = _start_and_collect(monkeypatch, b"")
    assert disconnected.wait(2)
    worker.close()
    assert events == [{"type": "error", "code": "WORKER_DISCONNECTED"}]


def test_truncated_frame_header_reports_fatal_error(monkeypatch) -> None:
    """不足 4 字节的半截帧头按断开收敛，不抛未捕获的 ``struct.error``。"""

    worker, events, disconnected = _start_and_collect(monkeypatch, b"\x00\x00")
    assert disconnected.wait(2)
    worker.close()
    assert events == [{"type": "error", "code": "WORKER_DISCONNECTED"}]


def test_event_handler_failure_is_logged_and_converges(monkeypatch, caplog) -> None:
    """事件回调出错：落 ERROR 日志并补发致命错误，后续帧不再分发。"""

    caplog.set_level(logging.ERROR)

    def explode(event):
        raise KeyError("screen")

    frames = _frame({"type": "output", "data_base64": "eA=="}) * 2
    worker, events, disconnected = _start_and_collect(monkeypatch, frames, explode)
    assert disconnected.wait(2)
    worker.close()
    assert [event["type"] for event in events] == ["output", "error"]
    assert any(
        record.getMessage() == "terminal_worker_event_handler_failed" for record in caplog.records
    )
