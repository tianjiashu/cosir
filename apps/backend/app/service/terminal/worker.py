"""Terminal Worker sidecar protocol client。

本模块只负责 backend 与独立 Terminal Worker 之间的本机进程边界。PTY/ConPTY
实现位于 Rust sidecar；backend 不直接调用平台 PTY API。Windows Worker 以无可见
控制台窗口的方式启动，shell 的交互语义仍由 Worker 内的 ConPTY 提供。
"""

from __future__ import annotations

import base64
import json
import os
import queue
import struct
import subprocess
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from app.config.logging.logger import log
from app.service.terminal.errors import (
    TerminalWorkerBackpressureError,
    TerminalWorkerUnavailableError,
)
from app.service.terminal.shell_resolver import ShellSpec

MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_INPUT_QUEUE_FRAMES = 64
MAX_INPUT_QUEUE_BYTES = 64 * 1024
WORKER_PROTOCOL = "terminal-worker-v1"
WORKER_ENV = "CODING_AGENT_TERMINAL_WORKER"
WorkerEventCallback = Callable[[Mapping[str, object]], None]


class TerminalWorker(Protocol):
    """Terminal Worker 生命周期和控制操作契约。"""

    @property
    def pid(self) -> int | None: ...

    def start(
        self,
        spec: ShellSpec,
        cwd: str,
        cols: int,
        rows: int,
        on_event: WorkerEventCallback,
    ) -> None: ...

    def write(self, data: bytes) -> None: ...

    def signal(self, signal_name: str) -> None: ...

    def close(self) -> None: ...


class TerminalWorkerFactory(Protocol):
    """创建一个尚未启动的 worker client。"""

    def create(self, instance_id: str) -> TerminalWorker: ...


class ProcessTerminalWorkerFactory:
    """通过 ``CODING_AGENT_TERMINAL_WORKER`` 创建 sidecar worker。"""

    def __init__(self, executable: str | None = None) -> None:
        """保存显式路径；未提供时延迟读取环境变量，方便测试和桌面装配。"""

        self._executable = executable

    def create(self, instance_id: str) -> TerminalWorker:
        """构造 process worker；不在此处启动子进程。"""

        executable = self._executable or os.environ.get(WORKER_ENV, "").strip()
        if not executable:
            raise TerminalWorkerUnavailableError(
                f"{WORKER_ENV} is not configured; terminal worker sidecar is unavailable"
            )
        return ProcessTerminalWorker(executable, instance_id)


class ProcessTerminalWorker:
    """基于长度前缀 JSON 控制帧的 Terminal Worker client。

    Worker 进程必须先返回 ``handshake``，之后发送 ``output``、``status``、``exit``
    事件。二进制 PTY 数据使用 base64 字段；shell/PTY 资源由 worker 管理。
    """

    def __init__(self, executable: str, instance_id: str) -> None:
        """初始化未启动的 worker client。"""

        self._executable = executable
        self.instance_id = instance_id
        self._process: subprocess.Popen[bytes] | None = None
        self._on_event: WorkerEventCallback | None = None
        self._write_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._handshake_event = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._writer_thread: threading.Thread | None = None
        self._input_queue: queue.Queue[bytes] = queue.Queue(maxsize=MAX_INPUT_QUEUE_FRAMES)
        self._input_queue_lock = threading.Lock()
        self._input_queue_bytes = 0
        self._writer_stop = threading.Event()
        self._exit_lock = threading.Lock()
        self._exit_emitted = False
        self._handshake_valid = False

    @property
    def pid(self) -> int | None:
        """返回当前 sidecar PID。"""

        return self._process.pid if self._process is not None else None

    def start(
        self,
        spec: ShellSpec,
        cwd: str,
        cols: int,
        rows: int,
        on_event: WorkerEventCallback,
    ) -> None:
        """无可见控制台窗口地启动 worker，发送 shell spec 并等待 handshake。"""

        if self._process is not None:
            raise TerminalWorkerUnavailableError("terminal worker has already started")
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            process = subprocess.Popen(  # noqa: S603 - executable is desktop-provided sidecar
                [self._executable, "--stdio", "--instance-id", self.instance_id],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(Path(cwd).anchor or cwd),
                bufsize=0,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise TerminalWorkerUnavailableError(f"failed to start terminal worker: {exc}") from exc

        self._process = process
        self._on_event = on_event
        self._exit_emitted = False
        self._handshake_valid = False
        self._stop_event.clear()
        self._writer_stop.clear()
        self._reader_thread = threading.Thread(
            target=self._read_events,
            name=f"terminal-worker-reader-{self.instance_id[:8]}",
            daemon=True,
        )
        self._reader_thread.start()
        threading.Thread(
            target=self._drain_stderr,
            name="terminal-worker-stderr",
            daemon=True,
        ).start()
        self._send(
            {
                "type": "start",
                "shell": list(spec.argv),
                "shell_kind": spec.kind,
                "cwd": cwd,
                "cols": cols,
                "rows": rows,
            }
        )
        if not self._handshake_event.wait(timeout=5) or not self._handshake_valid:
            self.close()
            raise TerminalWorkerUnavailableError("terminal worker handshake failed")
        self._writer_thread = threading.Thread(
            target=self._write_events,
            name=f"terminal-worker-writer-{self.instance_id[:8]}",
            daemon=True,
        )
        self._writer_thread.start()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"terminal-worker-heartbeat-{self.instance_id[:8]}",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def write(self, data: bytes) -> None:
        """向 worker 写入一段 PTY input bytes。"""

        process = self._process
        if process is None or process.poll() is not None or not self._handshake_valid:
            raise TerminalWorkerUnavailableError("terminal worker is not ready")
        payload = self._encode_frame(
            {"type": "write", "data_base64": base64.b64encode(data).decode("ascii")}
        )
        with self._input_queue_lock:
            if (
                self._input_queue.full()
                or self._input_queue_bytes + len(data) > MAX_INPUT_QUEUE_BYTES
            ):
                raise TerminalWorkerBackpressureError("terminal worker input queue is full")
            self._input_queue.put_nowait(payload)
            self._input_queue_bytes += len(data)

    def signal(self, signal_name: str) -> None:
        """发送平台无关的 signal 名称，由 worker 映射到平台语义。"""

        self._send({"type": "signal", "signal": signal_name})

    def close(self) -> None:
        """请求 worker 关闭 shell，超时后终止 worker 进程。"""

        process = self._process
        if process is None:
            return
        self._stop_event.set()
        self._writer_stop.set()
        with self._input_queue_lock:
            while True:
                try:
                    self._input_queue.get_nowait()
                    self._input_queue.task_done()
                except queue.Empty:
                    break
            self._input_queue_bytes = 0
        if process.poll() is None:
            with suppress(TerminalWorkerUnavailableError):
                self._send({"type": "shutdown"})
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        self._process = None

    def _write_events(self) -> None:
        """从有界 input queue 向 worker 写入控制帧。"""

        while not self._writer_stop.is_set():
            try:
                payload = self._input_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._send_payload(payload)
            except TerminalWorkerUnavailableError:
                return
            finally:
                with self._input_queue_lock:
                    self._input_queue_bytes = max(
                        0, self._input_queue_bytes - _payload_data_bytes(payload)
                    )
                self._input_queue.task_done()

    @staticmethod
    def _encode_frame(message: Mapping[str, object]) -> bytes:
        """编码长度前缀控制帧。"""

        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_FRAME_BYTES:
            raise TerminalWorkerUnavailableError("terminal worker frame exceeds size limit")
        return struct.pack(">I", len(payload)) + payload

    def _send_payload(self, frame: bytes) -> None:
        """向 worker stdin 写入已经编码的 frame。"""

        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise TerminalWorkerUnavailableError("terminal worker is not running")
        try:
            with self._write_lock:
                process.stdin.write(frame)
                process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise TerminalWorkerUnavailableError("terminal worker control pipe is closed") from exc

    def _send(self, message: Mapping[str, object]) -> None:
        """发送一个长度前缀控制帧。"""

        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise TerminalWorkerUnavailableError("terminal worker is not running")
        self._send_payload(self._encode_frame(message))

    def _read_events(self) -> None:
        """后台读取 worker 事件并转发给 session service。"""

        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while not self._stop_event.is_set():
                header = _read_exact(process.stdout, 4)
                if not header:
                    break
                size = struct.unpack(">I", header)[0]
                if size <= 0 or size > MAX_FRAME_BYTES:
                    raise TerminalWorkerUnavailableError("invalid terminal worker frame size")
                payload = _read_exact(process.stdout, size)
                if len(payload) != size:
                    break
                event = json.loads(payload.decode("utf-8"))
                if not isinstance(event, dict):
                    raise TerminalWorkerUnavailableError("terminal worker event is not an object")
                if event.get("type") == "handshake":
                    self._handshake_valid = self._validate_handshake(event, process.pid)
                    self._handshake_event.set()
                callback = self._on_event
                if callback is not None:
                    callback(event)
        except (
            OSError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            TerminalWorkerUnavailableError,
        ) as exc:
            if not self._stop_event.is_set():
                log.warning(
                    "terminal_worker_reader_failed",
                    extra={
                        "msg": "Terminal Worker 事件读取失败",
                        "data": {"error_type": type(exc).__name__},
                    },
                )
        finally:
            self._handshake_event.set()
            if not self._stop_event.is_set():
                self._emit_exit(process.poll())

    def _emit_exit(self, exit_code: int | None) -> None:
        """在 worker 自然退出或控制管道异常时通知 session service 一次。"""

        with self._exit_lock:
            if self._exit_emitted:
                return
            self._exit_emitted = True
        callback = self._on_event
        if callback is not None:
            callback({"type": "exit", "exit_code": exit_code})

    def _validate_handshake(self, event: Mapping[str, object], process_pid: int) -> bool:
        """校验 worker 协议版本、实例、PID 与 PTY 类型。"""

        return (
            event.get("protocol") == WORKER_PROTOCOL
            and event.get("instance_id") == self.instance_id
            and event.get("pid") == process_pid
            and event.get("pty_kind") in {"conpty", "unix_pty"}
        )

    def _drain_stderr(self) -> None:
        """消费 worker stderr，防止诊断管道阻塞；不把原始内容写入业务日志。"""

        process = self._process
        if process is None or process.stderr is None:
            return
        for _ in process.stderr:
            if self._stop_event.is_set():
                break

    def _heartbeat_loop(self) -> None:
        """按协议向 worker 发送 heartbeat。"""

        while not self._stop_event.wait(timeout=5):
            try:
                self._send({"type": "heartbeat"})
            except TerminalWorkerUnavailableError:
                return


def _read_exact(stream: object, size: int) -> bytes:
    """从 binary stream 读取最多指定字节数，EOF 时返回已读取内容。"""

    read = stream.read  # type: ignore[attr-defined]
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _payload_data_bytes(payload: bytes) -> int:
    """从 write frame 估算其原始 input bytes 大小。"""

    try:
        size = struct.unpack(">I", payload[:4])[0]
        message = json.loads(payload[4 : 4 + size].decode("utf-8"))
        encoded = message.get("data_base64") if isinstance(message, dict) else None
        return len(base64.b64decode(encoded, validate=True)) if isinstance(encoded, str) else 0
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return 0


def new_worker_instance_id() -> str:
    """生成不可复用的 worker instance id。"""

    return uuid.uuid4().hex
