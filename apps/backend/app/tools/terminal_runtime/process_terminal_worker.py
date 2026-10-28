"""Terminal Worker sidecar protocol client。

本模块只负责 backend 与独立 Terminal Worker 之间的本机进程边界。PTY/ConPTY
实现位于 Rust sidecar；backend 不直接调用平台 PTY API。Windows Worker 以无可见
控制台窗口的方式启动，shell 的交互语义仍由 Worker 内的 ConPTY 提供。
"""

from __future__ import annotations

import json
import os
import platform
import queue
import struct
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from app.config.constant import Constant
from app.config.logging.logger import log
from app.tools.terminal_runtime.errors import (
    TerminalWorkerBackpressureError,
    TerminalWorkerHandshakeError,
    TerminalWorkerProtocolError,
    TerminalWorkerSignalFailedError,
    TerminalWorkerSignalUnsupportedError,
    TerminalWorkerUnavailableError,
)
from app.tools.terminal_runtime.shell_resolver import ShellSpec

WorkerEventCallback = Callable[[Mapping[str, object]], None]
SIGNAL_ACK_TIMEOUT_SECONDS = 5.0
HANDSHAKE_TIMEOUT_SECONDS = 5.0


@dataclass
class _PendingSignal:
    """等待 worker signal_result 的单次请求状态。"""

    event: threading.Event
    result: Mapping[str, object] | None = None


class ProcessTerminalWorker:
    """基于长度前缀 JSON 控制帧的 Terminal Worker client。

    Worker 进程必须先返回 ``handshake``，之后发送 ``output``、``screen``、``shell``、``error``、
    ``exit`` 事件。二进制 PTY 数据使用 base64 字段；shell/PTY 资源由 worker 管理。

    事件回调在本对象的读线程内同步执行：回调方不得在其中做阻塞 IO（会话收尾、外部通知由
    service 移到其他线程）。
    """

    def __init__(self, instance_id: str) -> None:
        """构造时解析显式路径或宿主环境配置，不启动子进程。

        未配置路径时记录诊断并抛 TerminalWorkerUnavailableError；service 在创建
        会话时才构造本对象，后端启动不要求终端 sidecar 已装配。
        """

        executable = os.environ.get(Constant.Terminal.WORKER_ENV, "").strip()
        if not executable:
            log.warning(
                "terminal_worker_not_configured",
                extra={
                    "msg": "未配置 Terminal Worker 可执行文件，终端 session 无法创建",
                    "data": {
                        "env_var": Constant.Terminal.WORKER_ENV,
                        "instance_id": instance_id,
                    },
                },
            )
            raise TerminalWorkerUnavailableError(
                f"{Constant.Terminal.WORKER_ENV} is not configured; "
                "terminal worker sidecar is unavailable"
            )
        self._executable = executable
        self.instance_id = instance_id
        self._process: subprocess.Popen[bytes] | None = None
        self._on_event: WorkerEventCallback | None = None
        self._write_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._handshake_event = threading.Event()
        self._input_queue: queue.Queue[bytes] = queue.Queue(
            maxsize=Constant.Terminal.MAX_INPUT_QUEUE_FRAMES
        )
        self._input_queue_lock = threading.Lock()
        self._input_queue_bytes = 0
        self._handshake_valid = False
        self._handshake_payload: dict[str, object] | None = None
        self._capabilities = frozenset[str]()
        self.geometry: dict[str, int] = {}
        self._signal_lock = threading.Lock()
        self._pending_signals: dict[str, _PendingSignal] = {}

    @property
    def pid(self) -> int | None:
        """返回当前 sidecar PID。"""

        return self._process.pid if self._process is not None else None

    @property
    def capabilities(self) -> frozenset[str]:
        """返回最近一次 handshake 声明的静态 worker 能力。"""

        return self._capabilities

    def start(
            self,
            spec: ShellSpec,
            cwd: str,
            on_event: WorkerEventCallback,
    ) -> None:
        """无可见控制台窗口地启动 worker，发送 shell spec 并等待 handshake。

        本对象一次性使用：每个会话构造一个 worker、只启动一次，``close`` 后不再重启（停止信号、
        握手状态都不复位）。``cwd`` 由调用方保证已通过路径安全解析；``on_event`` 接收
        ``output`` / ``screen`` / ``shell`` / ``error`` / ``exit`` 事件。

        异常:
            TerminalWorkerUnavailableError: worker 已启动过、可执行文件无法拉起、握手超时或握手前
                断开（可能是 worker 启动慢、瞬时崩溃等条件，可重试）。
            TerminalWorkerHandshakeError: 收到握手但实例 / PID / PTY 类型 / 尺寸不符，属确定性
                失败，重试同一 worker 二进制不会有不同结果。

        副作用:
            启动 worker 子进程与 reader / stderr / writer / heartbeat 线程；失败路径写 WARNING
            日志 ``terminal_worker_spawn_failed`` 或 ``terminal_worker_handshake_failed`` 并关闭
            已启动的进程。
        """

        if self._process is not None:
            raise TerminalWorkerUnavailableError("terminal worker has already started")
        try:
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NO_WINDOW
            else:
                creationflags = 0
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
            log.warning(
                "terminal_worker_spawn_failed",
                extra={
                    "msg": "Terminal Worker 启动失败，终端 session 无法创建",
                    "data": {
                        "executable": self._executable,
                        "instance_id": self.instance_id,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                },
                exc_info=True,
            )
            raise TerminalWorkerUnavailableError(f"failed to start terminal worker: {exc}") from exc

        self._process = process
        self._on_event = on_event
        threading.Thread(
            target=self._read_events,
            name=f"terminal-worker-reader-{self.instance_id[:8]}",
            daemon=True,
        ).start()
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
                "integration": spec.integration,
            }
        )
        # 超时、握手前断开与「收到但不合法」要分开：前两者可能是 worker 启动慢或瞬时崩溃
        # （可重试），后者是确定性协议不兼容（重试同一二进制必然失败），各自留下可定位的日志。
        handshake_expired = not self._handshake_event.wait(timeout=HANDSHAKE_TIMEOUT_SECONDS)
        if handshake_expired or not self._handshake_valid:
            if handshake_expired:
                reason = "timeout"
            elif self._handshake_payload is None:
                reason = "disconnected"
            else:
                reason = "rejected"
            self._log_handshake_failure(reason)
            self.close()
            if reason == "timeout":
                raise TerminalWorkerUnavailableError(
                    f"terminal worker handshake timed out after {HANDSHAKE_TIMEOUT_SECONDS:g}s"
                )
            if reason == "disconnected":
                raise TerminalWorkerUnavailableError("terminal worker exited before handshake")
            raise TerminalWorkerHandshakeError(self._describe_handshake_mismatch())
        threading.Thread(
            target=self._write_events,
            name=f"terminal-worker-writer-{self.instance_id[:8]}",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._heartbeat_loop,
            name=f"terminal-worker-heartbeat-{self.instance_id[:8]}",
            daemon=True,
        ).start()

    def write(self, input: Mapping[str, object]) -> None:
        """将已校验的单次输入（line / keys / raw）入队；worker 负责编码并一次写入 PTY。"""

        self._enqueue({"type": "write", "input": dict(input)})

    def resize(self, rows: int, columns: int) -> None:
        """与输入共用有序队列，保证 resize 与前后按键的相对顺序。

        尺寸校验失败由 worker 报非致命错误。
        """

        self._enqueue({"type": "resize", "rows": rows, "cols": columns})

    def _enqueue(self, message: Mapping[str, object]) -> None:
        """非阻塞入队；队列满抛背压错误，调用方（含事件循环里的 WS）不会被阻塞。

        write / resize / signal 都经这条队列由写线程按序发出，保证相对顺序；heartbeat 与
        shutdown 是控制面帧，直接写。
        """

        process = self._process
        if process is None or process.poll() is not None or not self._handshake_valid:
            raise TerminalWorkerUnavailableError("terminal worker is not ready")
        payload = self._encode_frame(message)
        with self._input_queue_lock:
            queued_bytes = self._input_queue_bytes + len(payload)
            if (
                    self._input_queue.full()
                    or queued_bytes > Constant.Terminal.MAX_INPUT_QUEUE_BYTES
            ):
                raise TerminalWorkerBackpressureError("terminal worker input queue is full")
            self._input_queue.put_nowait(payload)
            self._input_queue_bytes += len(payload)

    def signal(self, signal_name: str) -> None:
        """经输入同一有序队列发送 signal，并等待 worker 确认实际投递结果。

        不能直接写 stdin：队列里可能还排着先前入队的 line / keys，越过它们会让中断先于命令到达。
        队列满时抛 ``TerminalWorkerBackpressureError``（与输入一致）。回执由读线程接收，调用方
        不得持有会被读线程回调获取的锁。
        """

        request_id = uuid.uuid4().hex
        pending = _PendingSignal(threading.Event())
        with self._signal_lock:
            self._pending_signals[request_id] = pending
        try:
            self._enqueue({"type": "signal", "request_id": request_id, "signal": signal_name})
        except Exception:
            with self._signal_lock:
                self._pending_signals.pop(request_id, None)
            raise
        if not pending.event.wait(timeout=SIGNAL_ACK_TIMEOUT_SECONDS):
            with self._signal_lock:
                self._pending_signals.pop(request_id, None)
            raise TerminalWorkerUnavailableError("terminal worker signal acknowledgement timed out")
        with self._signal_lock:
            result = self._pending_signals.pop(request_id, None)
        if result is None or result.result is None:
            raise TerminalWorkerUnavailableError("terminal worker signal result is missing")
        status = result.result.get("status")
        if status == "applied":
            return
        if status == "unsupported":
            raise TerminalWorkerSignalUnsupportedError(
                str(result.result.get("reason", "unsupported"))
            )
        if status == "failed":
            raise TerminalWorkerSignalFailedError("terminal worker failed to deliver signal")
        raise TerminalWorkerUnavailableError("terminal worker signal result is missing")

    def close(self) -> None:
        """请求 worker 关闭 shell，超时后终止 worker 进程。"""

        process = self._process
        if process is None:
            return
        self._stop_event.set()
        with self._signal_lock:
            for pending in self._pending_signals.values():
                pending.result = {"status": "unavailable"}
                pending.event.set()
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

        while not self._stop_event.is_set():
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
                    self._input_queue_bytes = max(0, self._input_queue_bytes - len(payload))
                self._input_queue.task_done()

    @staticmethod
    def _encode_frame(message: Mapping[str, object]) -> bytes:
        """编码长度前缀控制帧。"""

        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(payload) > Constant.Terminal.MAX_FRAME_BYTES:
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
        """发送一个长度前缀控制帧；进程存活检查在 ``_send_payload``。"""

        self._send_payload(self._encode_frame(message))

    def _read_events(self) -> None:
        """后台读取 worker 事件并转发给 session service。

        shell 的结束只以 worker 自己发出的 ``exit`` 为准（worker 的每条退出路径都恰好发一次）。
        读线程在未被 ``close`` 时结束，说明 worker 异常断开、协议违约或事件回调出错，一律补发
        致命错误 ``WORKER_DISCONNECTED``，由会话收敛为 ``failed``；不能伪装成 ``exit``，否则崩溃会被
        记成 shell 正常退出。worker 已发 exit 后 EOF 也会补发，会话终态已定、迁移幂等，不改写结果。
        握手前断开不补发：由 ``start`` 抛错、service 记为启动失败，只此一处。
        """

        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while not self._stop_event.is_set():
                header = self._read_exact(process.stdout, 4)
                if len(header) != 4:
                    break
                size = struct.unpack(">I", header)[0]
                if size <= 0 or size > Constant.Terminal.MAX_FRAME_BYTES:
                    raise TerminalWorkerProtocolError(f"invalid terminal worker frame size: {size}")
                payload = self._read_exact(process.stdout, size)
                if len(payload) != size:
                    break
                event = json.loads(payload.decode("utf-8"))
                if not isinstance(event, dict):
                    raise TerminalWorkerProtocolError("terminal worker event is not an object")
                if event.get("type") == "handshake":
                    # 保留原始握手内容：校验不通过时由 start() 输出期望/实际字段对照与日志。
                    self._handshake_payload = dict(event)
                    self._handshake_valid = self._validate_handshake(event, process.pid)
                    raw_capabilities = event.get("capabilities")
                    if isinstance(raw_capabilities, list):
                        self._capabilities = frozenset(
                            value for value in raw_capabilities if isinstance(value, str)
                        )
                    if self._handshake_valid:
                        self.geometry = {"rows": event["rows"], "columns": event["columns"]}
                    self._handshake_event.set()
                if event.get("type") == "signal_result":
                    request_id = event.get("request_id")
                    if isinstance(request_id, str):
                        with self._signal_lock:
                            pending = self._pending_signals.get(request_id)
                            if pending is not None:
                                pending.result = event
                                pending.event.set()
                    continue
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
                        "data": {
                            "instance_id": self.instance_id,
                            "worker_pid": process.pid,
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                        },
                    },
                )
        except Exception:
            # 事件回调（会话侧内存更新）出错是本仓缺陷：落日志后按断开收敛，不让线程无声崩掉。
            log.exception(
                "terminal_worker_event_handler_failed",
                extra={
                    "msg": "Terminal Worker 事件处理失败，会话将收敛为 failed",
                    "data": {"instance_id": self.instance_id, "worker_pid": process.pid},
                },
            )
        finally:
            self._handshake_event.set()
            callback = self._on_event
            if not self._stop_event.is_set() and self._handshake_valid and callback is not None:
                callback({"type": "error", "code": "WORKER_DISCONNECTED"})

    def _validate_handshake(self, event: Mapping[str, object], process_pid: int) -> bool:
        """校验进程外握手的身份、PTY 类型与正整数逻辑尺寸。"""

        rows, columns = event.get("rows"), event.get("columns")
        return (
                event.get("instance_id") == self.instance_id
                and event.get("pid") == process_pid
                and event.get("pty_kind") in {"conpty", "unix_pty"}
                and type(rows) is int
                and rows > 0
                and type(columns) is int
                and columns > 0
        )

    def _handshake_mismatch_details(self) -> dict[str, object]:
        """握手「期望值 vs 实际收到值」对照（仅协议元数据）；未收到握手帧时实际值为 ``None``。"""

        received = self._handshake_payload or {}
        process = self._process
        return {
            "expected_instance_id": self.instance_id,
            "received_instance_id": received.get("instance_id"),
            "expected_pid": process.pid if process is not None else None,
            "received_pid": received.get("pid"),
            "received_pty_kind": received.get("pty_kind"),
            "received_rows": received.get("rows"),
            "received_columns": received.get("columns"),
        }

    def _describe_handshake_mismatch(self) -> str:
        """构造握手被拒时的错误消息，列出期望与实际身份元数据。"""

        details = self._handshake_mismatch_details()
        rendered = ", ".join(f"{key}={value!r}" for key, value in details.items())
        return (
            f"terminal worker handshake rejected by {self._executable}: {rendered}; "
            "verify that the configured worker executable belongs to this application"
        )

    def _log_handshake_failure(self, reason: str) -> None:
        """记录握手失败与协议字段对照；reason 取 timeout / disconnected / rejected。"""

        process = self._process
        log.warning(
            "terminal_worker_handshake_failed",
            extra={
                "msg": "Terminal Worker 握手失败，终端 session 无法启动",
                "data": {
                    "reason": reason,
                    "executable": self._executable,
                    "instance_id": self.instance_id,
                    "worker_pid": process.pid if process is not None else None,
                    "timeout_seconds": HANDSHAKE_TIMEOUT_SECONDS,
                    **self._handshake_mismatch_details(),
                },
            },
        )

    def _drain_stderr(self) -> None:
        """消费并限长记录 worker stderr，防止诊断管道阻塞。"""

        process = self._process
        if process is None or process.stderr is None:
            return
        for raw_line in process.stderr:
            line = self._sanitize_worker_stderr(raw_line)
            if line:
                log.warning(
                    "terminal_worker_stderr",
                    extra={
                        "msg": "Terminal Worker stderr",
                        "data": {
                            "worker_instance_id": self.instance_id,
                            "worker_pid": process.pid,
                            "platform": platform.system().lower(),
                            "error_category": "worker_stderr",
                            "line": line,
                        },
                    },
                )

    def _heartbeat_loop(self) -> None:
        """按协议向 worker 发送 heartbeat。"""

        while not self._stop_event.wait(timeout=5):
            try:
                self._send({"type": "heartbeat"})
            except TerminalWorkerUnavailableError:
                return

    def _read_exact(self, stream: object, size: int) -> bytes:
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

    def _sanitize_worker_stderr(self, raw_line: bytes, *, max_length: int = 4096) -> str:
        """将 worker 诊断行转换为受限、可落盘的日志文本。"""

        text = raw_line.decode("utf-8", errors="replace").strip()
        sanitized = "".join(
            character for character in text if character >= " " or character == "\t"
        )
        return sanitized[:max_length]
