"""本机 Terminal session 领域服务。

该服务拥有 session registry、PTY worker client、输出 seq/ring buffer 和只读预览订阅。
它不注册 Agent tool、不承载 Assistant UI 类型，也不把 PTY 输出写入 conversation snapshot。
"""

from __future__ import annotations

import base64
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from sqlalchemy.orm import Session

from app.config.logging.logger import log
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.models.terminal_session_record import TerminalSessionRecord
from app.service.terminal.errors import (
    TerminalSessionCapacityError,
    TerminalSessionError,
    TerminalSessionNotFoundError,
    TerminalSessionOwnershipError,
    TerminalSessionResyncRequiredError,
    TerminalSessionStateError,
    TerminalWorkerUnavailableError,
)
from app.service.terminal.shell_resolver import ShellResolver
from app.service.terminal.worker import (
    ProcessTerminalWorkerFactory,
    TerminalWorker,
    TerminalWorkerFactory,
    new_worker_instance_id,
)
from app.storage.crud.terminal_session_crud import TerminalSessionCrud
from app.utils.datetime_utils import to_text, utc_now

MAX_ACTIVE_SESSIONS = 32
MAX_RING_BUFFER_BYTES = 1024 * 1024
MAX_PREVIEW_QUEUE_FRAMES = 256
MAX_PREVIEW_QUEUE_BYTES = 1024 * 1024
MAX_AGENT_INPUT_BYTES = 64 * 1024
MAX_AGENT_READ_BYTES = 64 * 1024
MAX_WORKER_OUTPUT_CHUNK_BYTES = 16 * 1024
DEFAULT_MAX_LIFETIME_SECONDS = 8 * 60 * 60
DEFAULT_IDLE_TIMEOUT_SECONDS = 30 * 60
SWEEPER_INTERVAL_SECONDS = 30

RunCancellationCheck = Callable[[], bool]


@dataclass(frozen=True)
class TerminalReadResult:
    """按 cursor 返回的非破坏性输出读取结果。"""

    output: tuple[dict[str, object], ...]
    next_seq: int
    first_available_seq: int
    status: str
    cols: int
    rows: int
    truncated: bool

    def to_dict(self) -> dict[str, object]:
        """转换为工具/API 可序列化的 UTF-8 文本视图。

        session 内部和 WebSocket 仍保留 ``data_base64`` 原始 bytes；Agent tool
        返回 ``data`` 文本并以 replacement character 处理非 UTF-8 字节，避免把
        transport 编码细节暴露给模型。
        """

        return {
            "output": [_agent_output_frame(frame) for frame in self.output],
            "next_seq": self.next_seq,
            "first_available_seq": self.first_available_seq,
            "status": self.status,
            "cols": self.cols,
            "rows": self.rows,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class TerminalPreviewAttachment:
    """一次预览 attach 的初始快照和 subscriber。"""

    attached: dict[str, object]
    replay: tuple[dict[str, object], ...]
    terminal_event: dict[str, object] | None
    subscription: TerminalPreviewSubscription


class TerminalPreviewSubscription:
    """一个只读 WebSocket subscriber 的线程安全有界队列。"""

    def __init__(self) -> None:
        """初始化尚未激活的 replay subscription。"""

        import queue

        self._queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=MAX_PREVIEW_QUEUE_FRAMES)
        self._queue_bytes = 0
        self._lock = threading.Lock()
        self._replaying = True
        self._pending: list[dict[str, object]] = []
        self._pending_bytes = 0
        self._closed = False
        self._overflowed = False

    @property
    def overflowed(self) -> bool:
        """返回 subscriber 是否因背压失去连续输出。"""

        with self._lock:
            return self._overflowed

    def publish(self, event: dict[str, object]) -> None:
        """尽力投递一个事件；队列满时标记 overflow，不阻塞 PTY reader。"""

        with self._lock:
            if self._closed:
                return
            if self._overflowed:
                return
            if self._replaying:
                if len(self._pending) >= MAX_PREVIEW_QUEUE_FRAMES:
                    self._mark_overflow_locked()
                    return
                self._pending.append(event)
                self._pending_bytes += _event_size(event)
                if self._pending_bytes > MAX_PREVIEW_QUEUE_BYTES:
                    self._mark_overflow_locked()
                return
            try:
                event_bytes = _event_size(event)
                if self._queue_bytes + event_bytes > MAX_PREVIEW_QUEUE_BYTES:
                    self._mark_overflow_locked()
                    return
                self._queue.put_nowait(event)
                self._queue_bytes += event_bytes
            except Exception:
                self._mark_overflow_locked()

    def activate(self) -> None:
        """完成 replay，按原顺序释放 replay 期间积压的 live 事件。"""

        with self._lock:
            if self._closed:
                return
            self._replaying = False
            for event in self._pending:
                try:
                    self._queue.put_nowait(event)
                    self._queue_bytes += _event_size(event)
                except Exception:
                    self._mark_overflow_locked()
                    break
            self._pending.clear()
            self._pending_bytes = 0

    def get(self, timeout: float) -> dict[str, object] | None:
        """阻塞读取一个预览事件，超时返回 ``None``。"""

        if self._closed:
            return None
        try:
            event = self._queue.get(timeout=timeout)
            with self._lock:
                self._queue_bytes = max(0, self._queue_bytes - _event_size(event))
            return event
        except Exception:
            return None

    def close(self) -> None:
        """关闭 subscriber，不影响 session 或其他 subscriber。"""

        with self._lock:
            self._closed = True
            self._pending.clear()
            self._pending_bytes = 0
            self._queue_bytes = 0

    def _mark_overflow_locked(self) -> None:
        """在持有 subscriber lock 时标记输出 gap。"""

        self._overflowed = True
        self._pending.clear()
        self._pending_bytes = 0
        while True:
            try:
                self._queue.get_nowait()
            except Exception:
                break
        self._queue_bytes = 0
        with suppress(Exception):
            marker: dict[str, object] = {"type": "subscriber_overflow"}
            self._queue.put_nowait(marker)
            self._queue_bytes = _event_size(marker)


class _SessionRuntime:
    """单个 session 的进程内运行态。"""

    def __init__(self, record: TerminalSessionRecord, worker: TerminalWorker | None) -> None:
        self.record = record
        self.worker = worker
        self.generation = record.worker_instance_id
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.frames: deque[dict[str, object]] = deque()
        self.buffer_bytes = 0
        self.next_seq = 1
        self.subscribers: set[TerminalPreviewSubscription] = set()
        self.last_touch_monotonic = 0.0

    @property
    def status(self) -> str:
        """返回当前进程内 session 状态。"""

        return self.record.status


class TerminalSessionService:
    """管理本机 Terminal session 的创建、Agent 操作和只读预览。

    该服务是 backend 进程内唯一的 session registry owner。它只接受来自 Agent tool
    handler 或 backend API 的调用；前端 WebSocket 只能订阅输出，不能通过本服务改变
    PTY 状态。session 元数据由 ``TerminalSessionCrud`` 持久化，PTY 和 ring buffer
    不跨 backend 重启恢复。
    """

    def __init__(
        self,
        crud: TerminalSessionCrud | None = None,
        worker_factory: TerminalWorkerFactory | None = None,
        shell_resolver: ShellResolver | None = None,
        *,
        max_active_sessions: int = MAX_ACTIVE_SESSIONS,
        ring_buffer_bytes: int = MAX_RING_BUFFER_BYTES,
    ) -> None:
        """创建服务；不启动 worker。"""

        self._crud = crud
        self._worker_factory = worker_factory or ProcessTerminalWorkerFactory()
        self._shell_resolver = shell_resolver or ShellResolver()
        self._max_active_sessions = max_active_sessions
        self._ring_buffer_bytes = ring_buffer_bytes
        self._registry: dict[str, _SessionRuntime] = {}
        self._registry_lock = threading.RLock()
        self._accepting = True
        self._sweeper_stop = threading.Event()
        self._sweeper_thread: threading.Thread | None = None

    def initialize(self) -> list[str]:
        """执行 backend 启动期 orphan recovery。"""

        if self._crud is None:
            recovered = []
        else:
            recovered = self._crud.recover_active()
            if recovered:
                log.info(
                    "terminal_sessions_recovered_after_restart",
                    extra={
                        "msg": "启动时收敛遗留 terminal session",
                        "data": {"session_ids": recovered},
                    },
                )
        if self._sweeper_thread is None:
            self._sweeper_stop.clear()
            self._sweeper_thread = threading.Thread(
                target=self._sweep_expired,
                name="terminal-session-sweeper",
                daemon=True,
            )
            self._sweeper_thread.start()
        return recovered

    def start(
        self,
        *,
        task_id: int,
        workspace_id: int,
        workspace_root: str,
        shell: str = "auto",
        cwd: str | None = None,
        cols: int = 120,
        rows: int = 32,
        created_by_run_id: int | None = None,
    ) -> dict[str, object]:
        """创建一个由 Agent 驱动的交互式 shell session。"""

        self._ensure_accepting()
        _validate_initial_dimensions(cols, rows)
        cwd_path = self._resolve_cwd(workspace_root, cwd)
        shell_spec = self._shell_resolver.resolve(shell)
        instance_id = new_worker_instance_id()
        session_id = f"term_{uuid.uuid4().hex}"
        now = utc_now()
        record = TerminalSessionRecord(
            session_id=session_id,
            task_id=task_id,
            workspace_id=workspace_id,
            created_by_run_id=created_by_run_id,
            initial_cwd=str(cwd_path),
            shell_kind=shell_spec.kind,
            shell_executable=shell_spec.executable,
            worker_instance_id=instance_id,
            worker_pid=None,
            status="starting",
            end_reason=None,
            exit_code=None,
            cols=cols,
            rows=rows,
            created_at=now,
            updated_at=now,
            last_activity_at=now,
            ended_at=None,
        )
        with self._registry_lock:
            self._ensure_capacity_locked()
            persisted = self._crud_create(record)
            try:
                worker = self._worker_factory.create(instance_id)
            except Exception as exc:
                self._persist_start_failure(
                    record,
                    f"worker_create_failed: {type(exc).__name__}",
                )
                raise
            runtime = _SessionRuntime(persisted, worker)
            self._registry[session_id] = runtime
        try:
            worker.start(
                shell_spec,
                str(cwd_path),
                cols,
                rows,
                lambda event: self._on_worker_event(session_id, event),
            )
            self._update_runtime(runtime, status="running", worker_pid=worker.pid)
        except Exception as exc:
            self._fail_runtime(runtime, f"worker_start_failed: {type(exc).__name__}")
            with suppress(Exception):
                worker.close()
            with self._registry_lock:
                self._registry.pop(session_id, None)
            raise
        return self.snapshot(session_id, task_id=task_id)

    def write(
        self,
        session_id: str,
        *,
        task_id: int,
        data: bytes,
        after_seq: int | None = None,
        wait_ms: int = 0,
        is_cancelled: RunCancellationCheck | None = None,
    ) -> TerminalReadResult:
        """向 session 写入 Agent input，并可短暂等待 output 增量。"""

        if not data:
            raise TerminalSessionError("terminal input must not be empty")
        if len(data) > MAX_AGENT_INPUT_BYTES:
            raise TerminalSessionError("terminal input exceeds 64 KiB")
        runtime = self._require_runtime(session_id, task_id)
        with runtime.lock:
            self._ensure_mutable(runtime)
            worker = runtime.worker
            if worker is None:
                raise TerminalSessionStateError("terminal session has no active worker")
            try:
                worker.write(data)
            except TerminalWorkerUnavailableError:
                self._fail_runtime(runtime, "worker_unavailable")
                raise
            self._touch(runtime)
        return self.read(
            session_id,
            task_id=task_id,
            after_seq=after_seq,
            wait_ms=wait_ms,
            is_cancelled=is_cancelled,
        )

    def read(
        self,
        session_id: str,
        *,
        task_id: int,
        after_seq: int | None = None,
        wait_ms: int = 1000,
        is_cancelled: RunCancellationCheck | None = None,
    ) -> TerminalReadResult:
        """按 output cursor 非破坏性读取 session 输出。"""

        runtime = self._require_runtime(session_id, task_id)
        wait_seconds = max(0, min(wait_ms, 30_000)) / 1000
        deadline = time.monotonic() + wait_seconds
        with runtime.condition:
            while True:
                self._validate_cursor(runtime, after_seq)
                result = self._read_locked(runtime, after_seq)
                if result.output or runtime.status in {"exited", "interrupted", "failed", "closed"}:
                    return result
                if is_cancelled is not None and is_cancelled():
                    return result
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return result
                runtime.condition.wait(timeout=min(remaining, 0.05))

    def signal(self, session_id: str, *, task_id: int, signal_name: str) -> dict[str, object]:
        """由 Agent 向 PTY 发送平台无关的 signal。"""

        if signal_name not in {"interrupt", "eof", "suspend"}:
            raise TerminalSessionError(f"unsupported terminal signal: {signal_name}")
        runtime = self._require_runtime(session_id, task_id)
        with runtime.lock:
            self._ensure_mutable(runtime)
            if runtime.worker is None:
                raise TerminalSessionStateError("terminal session has no active worker")
            try:
                runtime.worker.signal(signal_name)
            except TerminalWorkerUnavailableError:
                self._fail_runtime(runtime, "worker_unavailable")
                raise
            self._touch(runtime)
        return self.snapshot(session_id, task_id=task_id)

    def close(
        self,
        session_id: str,
        *,
        task_id: int,
        reason: str = "agent_closed",
    ) -> dict[str, object]:
        """显式关闭 session；重复关闭只返回当前终态。"""

        runtime = self._require_runtime(session_id, task_id)
        with runtime.lock:
            if runtime.status in {"closed", "exited", "interrupted", "failed"}:
                return self.snapshot(session_id, task_id=task_id)
            worker = runtime.worker
            self._update_runtime(
                runtime,
                status="closed",
                end_reason=reason,
                ended_at=utc_now(),
            )
            runtime.condition.notify_all()
        if worker is not None:
            worker.close()
        self._publish_status(runtime)
        return self.snapshot(session_id, task_id=task_id)

    def snapshot(self, session_id: str, *, task_id: int) -> dict[str, object]:
        """读取 session 当前元数据和 cursor watermark。"""

        runtime = self._require_runtime(session_id, task_id)
        with runtime.lock:
            first_available = _first_available_seq(runtime)
            return {
                **runtime.record.to_dict(),
                "generation": runtime.generation,
                "first_available_seq": first_available,
                "next_seq": runtime.next_seq,
            }

    def subscribe(
        self,
        session_id: str,
        *,
        task_id: int,
        after_seq: int | None,
    ) -> TerminalPreviewAttachment:
        """注册只读预览 subscriber，并返回有序 replay 边界。"""

        runtime = self._require_runtime(session_id, task_id)
        subscription = TerminalPreviewSubscription()
        with runtime.lock:
            self._validate_cursor(runtime, after_seq)
            first_available = _first_available_seq(runtime)
            replay = tuple(
                frame
                for frame in runtime.frames
                if after_seq is None or _frame_seq(frame) > after_seq
            )
            attached = {
                "protocol": "terminal-preview-v1",
                "type": "attached",
                "session_id": session_id,
                "task_id": task_id,
                "generation": runtime.generation,
                "first_available_seq": first_available,
                "next_seq": runtime.next_seq,
                "status": runtime.status,
                "cols": runtime.record.cols,
                "rows": runtime.record.rows,
            }
            terminal_event = (
                _terminal_event(runtime)
                if runtime.status in {"exited", "interrupted", "failed", "closed"}
                else None
            )
            runtime.subscribers.add(subscription)
        return TerminalPreviewAttachment(attached, replay, terminal_event, subscription)

    def unsubscribe(self, session_id: str, subscription: TerminalPreviewSubscription) -> None:
        """移除一个预览 subscriber。"""

        with self._registry_lock:
            runtime = self._registry.get(session_id)
        if runtime is not None:
            with runtime.lock:
                runtime.subscribers.discard(subscription)
        subscription.close()

    def shutdown(self) -> None:
        """停止接收新操作并关闭当前进程持有的全部 worker。"""

        with self._registry_lock:
            self._accepting = False
            runtimes = list(self._registry.values())
        for runtime in runtimes:
            if runtime.status in {"starting", "running"}:
                try:
                    self.close(
                        runtime.record.session_id,
                        task_id=runtime.record.task_id,
                        reason="backend_shutdown",
                    )
                except Exception:
                    log.exception(
                        "terminal_session_shutdown_failed",
                        extra={
                            "msg": "Terminal session 关闭失败",
                            "data": {"session_id": runtime.record.session_id},
                        },
                    )
        for runtime in runtimes:
            with runtime.lock:
                for subscriber in tuple(runtime.subscribers):
                    subscriber.close()
                runtime.subscribers.clear()
        with self._registry_lock:
            self._registry.clear()
        self._sweeper_stop.set()
        if (
            self._sweeper_thread is not None
            and self._sweeper_thread is not threading.current_thread()
        ):
            self._sweeper_thread.join(timeout=2)
        self._sweeper_thread = None

    def delete_task_sessions(
        self,
        task_ids: Iterable[int],
        session: Session | None = None,
    ) -> int:
        """在 Task 删除前关闭 worker，并在调用方事务中删除 session 元数据。

        该方法不会调用 ``close`` 的独立数据库事务，避免和 Task 级删除事务互相锁定；
        session 行会随调用方事务删除，worker/subscriber 则在进程内立即释放。
        """

        ids = set(task_ids)
        if not ids:
            return 0
        with self._registry_lock:
            runtimes = [
                runtime for runtime in self._registry.values() if runtime.record.task_id in ids
            ]
            for runtime in runtimes:
                self._registry.pop(runtime.record.session_id, None)
        for runtime in runtimes:
            with runtime.lock:
                worker = runtime.worker
                for subscriber in tuple(runtime.subscribers):
                    subscriber.close()
                runtime.subscribers.clear()
            if worker is not None:
                try:
                    worker.close()
                except Exception:
                    log.exception(
                        "terminal_session_task_delete_worker_close_failed",
                        extra={
                            "msg": "Task 删除时 Terminal Worker 关闭失败",
                            "data": {"session_id": runtime.record.session_id},
                        },
                    )
        if self._crud is None:
            return 0
        return self._crud.delete_by_task_ids(ids, session)

    def _on_worker_event(self, session_id: str, event: object) -> None:
        """将 worker 事件投影到 session runtime。"""

        if not isinstance(event, dict):
            return
        with self._registry_lock:
            runtime = self._registry.get(session_id)
        if runtime is None:
            return
        event_type = event.get("type")
        if event_type == "handshake":
            return
        if event_type == "output":
            self._on_output(runtime, event)
        elif event_type == "status":
            self._on_status(runtime, event)
        elif event_type == "error":
            self._on_worker_error(runtime, event)
        elif event_type == "exit":
            self._on_exit(runtime, event)

    def _on_output(self, runtime: _SessionRuntime, event: dict[str, object]) -> None:
        """接收 worker output 并广播到 ring buffer/subscribers。"""

        encoded = event.get("data_base64")
        if not isinstance(encoded, str):
            return
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            self._fail_runtime(runtime, "invalid_worker_output")
            return
        if not data or len(data) > MAX_WORKER_OUTPUT_CHUNK_BYTES:
            self._fail_runtime(runtime, "worker_output_chunk_too_large")
            return
        with runtime.condition:
            if runtime.status not in {"starting", "running"}:
                return
            frame = {
                "protocol": "terminal-preview-v1",
                "type": "output",
                "session_id": runtime.record.session_id,
                "generation": runtime.generation,
                "seq": runtime.next_seq,
                "data_base64": base64.b64encode(data).decode("ascii"),
            }
            runtime.next_seq += 1
            runtime.frames.append(frame)
            runtime.buffer_bytes += len(data)
            while runtime.buffer_bytes > self._ring_buffer_bytes and runtime.frames:
                removed = runtime.frames.popleft()
                runtime.buffer_bytes -= _frame_bytes(removed)
            for subscriber in tuple(runtime.subscribers):
                subscriber.publish(frame)
            runtime.condition.notify_all()
        self._touch(runtime)

    def _on_status(self, runtime: _SessionRuntime, event: dict[str, object]) -> None:
        """处理 worker 的初始 status 事件并同步 session 元数据。"""

        cols = _positive_int(event.get("cols"))
        rows = _positive_int(event.get("rows"))
        with runtime.condition:
            if cols is not None or rows is not None:
                self._update_runtime(runtime, cols=cols, rows=rows)
            self._publish_status(runtime)
            runtime.condition.notify_all()

    def _on_exit(self, runtime: _SessionRuntime, event: dict[str, object]) -> None:
        """把 worker 退出映射为唯一 session 终态。"""

        exit_code = event.get("exit_code")
        worker = runtime.worker
        with runtime.condition:
            if runtime.status in {"closed", "failed", "interrupted", "exited"}:
                return
            self._update_runtime(
                runtime,
                status="exited",
                end_reason="shell_exited",
                exit_code=exit_code if isinstance(exit_code, int) else None,
                ended_at=utc_now(),
            )
            self._publish_status(runtime)
            runtime.condition.notify_all()
        if worker is not None:
            with suppress(Exception):
                worker.close()

    def _on_worker_error(self, runtime: _SessionRuntime, event: dict[str, object]) -> None:
        """将 Worker 错误事件映射为失败或可记录的非致命运行态。"""

        code = event.get("code")
        if code == "START_ALREADY_COMPLETE" or code == "PTY_SIGNAL_FAILED":
            log.warning(
                "terminal_session_worker_nonfatal_error",
                extra={
                    "msg": "Terminal Worker 报告非致命错误",
                    "data": {"session_id": runtime.record.session_id, "code": code},
                },
            )
            return
        self._fail_runtime(runtime, _worker_error_reason(code))

    def _publish_status(self, runtime: _SessionRuntime) -> None:
        """向所有 subscriber 发布当前状态。"""

        event = {
            "protocol": "terminal-preview-v1",
            "type": "status",
            "session_id": runtime.record.session_id,
            "generation": runtime.generation,
            "status": runtime.status,
            "cols": runtime.record.cols,
            "rows": runtime.record.rows,
        }
        if runtime.status in {"exited", "interrupted", "failed", "closed"}:
            event = _terminal_event(runtime)
        for subscriber in tuple(runtime.subscribers):
            subscriber.publish(event)

    def _fail_runtime(self, runtime: _SessionRuntime, reason: str) -> None:
        """安全收敛 worker 失败，不向模型暴露原始异常。"""

        worker = runtime.worker
        with runtime.condition:
            if runtime.status in {"closed", "exited", "interrupted", "failed"}:
                return
            self._update_runtime(
                runtime,
                status="failed",
                end_reason=reason,
                ended_at=utc_now(),
            )
            runtime.condition.notify_all()
            self._publish_status(runtime)
        if worker is not None:
            with suppress(Exception):
                worker.close()

    def _require_runtime(self, session_id: str, task_id: int) -> _SessionRuntime:
        """取得 active runtime 或从持久化终态构造只读 runtime。"""

        with self._registry_lock:
            runtime = self._registry.get(session_id)
            if runtime is not None:
                if runtime.record.task_id != task_id:
                    raise TerminalSessionOwnershipError("terminal session does not belong to task")
                return runtime
        record = self._crud_get(session_id)
        if record.task_id != task_id:
            raise TerminalSessionOwnershipError("terminal session does not belong to task")
        if record.status in {"starting", "running"}:
            raise TerminalSessionStateError("terminal session is not available in this process")
        runtime = _SessionRuntime(record, None)
        with self._registry_lock:
            return self._registry.setdefault(session_id, runtime)

    def _resolve_cwd(self, workspace_root: str, cwd: str | None) -> Path:
        """解析并限制初始 cwd 到 workspace 内。"""

        resolver = PathResolver(workspace_root)
        resolved, error = resolver.resolve_within_workspace(cwd or ".")
        if resolved is None or not resolved.is_dir():
            raise TerminalSessionError(f"invalid terminal cwd: {error or cwd}")
        return resolved

    def _ensure_capacity_locked(self) -> None:
        """在 registry lock 下检查 active session 上限。"""

        active = sum(
            runtime.status in {"starting", "running"} for runtime in self._registry.values()
        )
        if active >= self._max_active_sessions:
            raise TerminalSessionCapacityError("maximum active terminal session count reached")

    def _ensure_accepting(self) -> None:
        """拒绝 backend shutdown 后的新操作。"""

        if not self._accepting:
            raise TerminalSessionStateError("terminal session service is shutting down")

    @staticmethod
    def _ensure_mutable(runtime: _SessionRuntime) -> None:
        """确认 session 仍可接受 Agent 控制操作。"""

        if runtime.status not in {"starting", "running"}:
            raise TerminalSessionStateError(f"terminal session is {runtime.status}")

    @staticmethod
    def _validate_cursor(runtime: _SessionRuntime, after_seq: int | None) -> None:
        """校验 cursor 是否仍在 ring buffer 可用范围内。"""

        if after_seq is not None and (isinstance(after_seq, bool) or after_seq < 0):
            raise TerminalSessionResyncRequiredError(
                after_seq or 0,
                _first_available_seq(runtime),
                runtime.next_seq,
            )
        first_available = _first_available_seq(runtime)
        if after_seq is not None and after_seq < first_available - 1:
            raise TerminalSessionResyncRequiredError(after_seq, first_available, runtime.next_seq)

    @staticmethod
    def _read_locked(runtime: _SessionRuntime, after_seq: int | None) -> TerminalReadResult:
        """在 session lock 下生成 cursor 增量。"""

        selected: list[dict[str, object]] = []
        selected_bytes = 0
        truncated = False
        for frame in runtime.frames:
            if after_seq is not None and _frame_seq(frame) <= after_seq:
                continue
            frame_bytes = _frame_bytes(frame)
            if selected and selected_bytes + frame_bytes > MAX_AGENT_READ_BYTES:
                truncated = True
                break
            selected.append(frame)
            selected_bytes += frame_bytes
        return TerminalReadResult(
            output=tuple(selected),
            next_seq=runtime.next_seq,
            first_available_seq=_first_available_seq(runtime),
            status=runtime.status,
            cols=runtime.record.cols,
            rows=runtime.record.rows,
            truncated=truncated,
        )

    def _touch(self, runtime: _SessionRuntime) -> None:
        """刷新 activity 元数据；失败不影响 PTY 主流程。"""

        now_datetime = utc_now()
        now = to_text(now_datetime)
        with runtime.lock:
            runtime.record = _replace_record(runtime.record, "last_activity_at", now_datetime)
            monotonic_now = time.monotonic()
            if monotonic_now - runtime.last_touch_monotonic < 1.0:
                return
            runtime.last_touch_monotonic = monotonic_now
            try:
                runtime.record = self._crud_update(runtime.record.session_id, last_activity_at=now)
            except Exception:
                log.warning(
                    "terminal_session_activity_persist_failed",
                    extra={
                        "msg": "Terminal session activity 持久化失败",
                        "data": {"session_id": runtime.record.session_id},
                    },
                )

    def _update_runtime(self, runtime: _SessionRuntime, **values: object) -> None:
        """更新进程内 record，并尽力持久化。"""

        current = runtime.record
        for name, value in values.items():
            if value is not None and hasattr(current, name):
                current = _replace_record(current, name, value)
        runtime.record = current
        try:
            runtime.record = self._crud_update(current.session_id, **values)
        except Exception:
            log.exception(
                "terminal_session_metadata_persist_failed",
                extra={
                    "msg": "Terminal session 元数据持久化失败",
                    "data": {"session_id": current.session_id},
                },
            )

    def _crud_create(self, record: TerminalSessionRecord) -> TerminalSessionRecord:
        """创建持久化记录；测试无 CRUD 时退化为内存记录。"""

        return self._crud.create(record) if self._crud is not None else record

    def _crud_get(self, session_id: str) -> TerminalSessionRecord:
        """读取持久化记录；无 CRUD 时只允许当前 registry。"""

        if self._crud is None:
            raise TerminalSessionNotFoundError(session_id)
        try:
            return self._crud.get(session_id)
        except KeyError as exc:
            raise TerminalSessionNotFoundError(session_id) from exc

    def _crud_update(self, session_id: str, **values: object) -> TerminalSessionRecord:
        """写入持久化 runtime 元数据；无 CRUD 时返回当前值。"""

        if self._crud is None:
            with self._registry_lock:
                runtime = self._registry.get(session_id)
            if runtime is None:
                raise TerminalSessionNotFoundError(session_id)
            return runtime.record
        return self._crud.update_runtime(session_id, **cast(Any, values))

    def _persist_start_failure(self, record: TerminalSessionRecord, reason: str) -> None:
        """把 worker 创建失败收敛成 failed，避免留下 starting 行。"""

        ended_at = utc_now()
        if self._crud is None:
            return
        try:
            self._crud.update_runtime(
                record.session_id,
                status="failed",
                end_reason=reason,
                ended_at=ended_at,
            )
        except Exception:
            log.exception(
                "terminal_session_start_failure_persist_failed",
                extra={
                    "msg": "Terminal session 启动失败状态持久化失败",
                    "data": {"session_id": record.session_id},
                },
            )

    def _sweep_expired(self) -> None:
        """定期关闭超出生命周期或 idle timeout 的 session。"""

        while not self._sweeper_stop.wait(SWEEPER_INTERVAL_SECONDS):
            now = utc_now()
            with self._registry_lock:
                runtimes = list(self._registry.values())
            for runtime in runtimes:
                with runtime.lock:
                    if runtime.status not in {"starting", "running"}:
                        continue
                    lifetime = (now - runtime.record.created_at).total_seconds()
                    idle = (now - runtime.record.last_activity_at).total_seconds()
                    reason = (
                        "max_lifetime_exceeded"
                        if lifetime >= DEFAULT_MAX_LIFETIME_SECONDS
                        else "idle_timeout"
                        if idle >= DEFAULT_IDLE_TIMEOUT_SECONDS
                        else None
                    )
                if reason is not None:
                    try:
                        self.close(
                            runtime.record.session_id,
                            task_id=runtime.record.task_id,
                            reason=reason,
                        )
                    except Exception:
                        log.exception(
                            "terminal_session_expiry_close_failed",
                            extra={
                                "msg": "Terminal session 超时关闭失败",
                                "data": {"session_id": runtime.record.session_id},
                            },
                        )

_FATAL_WORKER_ERROR_REASONS = {
    "HEARTBEAT_TIMEOUT": "worker_heartbeat_timeout",
    "INVALID_INPUT": "worker_invalid_input",
    "PTY_WRITE_FAILED": "worker_pty_write_failed",
    "PTY_WAIT_FAILED": "worker_pty_wait_failed",
}


def _worker_error_reason(code: object) -> str:
    """将 Worker 错误码转换为稳定、非敏感的 session 失败原因。"""

    if isinstance(code, str):
        return _FATAL_WORKER_ERROR_REASONS.get(code, "worker_failed")
    return "worker_failed"


def _replace_record(
    record: TerminalSessionRecord,
    name: str,
    value: object,
) -> TerminalSessionRecord:
    """不引入 dataclasses.replace 的动态类型绕过，复制 record 单个字段。"""

    from dataclasses import replace

    return replace(record, **cast(Any, {name: value}))


def _positive_int(value: object) -> int | None:
    """读取正整数事件字段。"""

    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _validate_initial_dimensions(cols: int, rows: int) -> None:
    """校验 PTY 创建时的初始尺寸；session 创建后不支持调整尺寸。"""

    if (
        isinstance(cols, bool)
        or isinstance(rows, bool)
        or not isinstance(cols, int)
        or not isinstance(rows, int)
        or cols < 20
        or rows < 5
        or cols > 500
        or rows > 200
    ):
        raise TerminalSessionError("terminal dimensions are out of range")


def _frame_bytes(frame: dict[str, object]) -> int:
    """返回 frame 对应的 payload 字节数。"""

    encoded = frame.get("data_base64")
    if not isinstance(encoded, str):
        return 0
    try:
        return len(base64.b64decode(encoded, validate=True))
    except (ValueError, TypeError):
        return 0


def _event_size(event: dict[str, object]) -> int:
    """返回 subscriber 队列的保守事件大小估算。"""

    return _frame_bytes(event) or 128


def _agent_output_frame(frame: dict[str, object]) -> dict[str, object]:
    """将内部 raw output frame 投影为 Agent 可读的 UTF-8 文本 frame。"""

    encoded = frame.get("data_base64")
    if not isinstance(encoded, str):
        return {"seq": frame.get("seq"), "data": ""}
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        data = b""
    return {
        "seq": frame.get("seq"),
        "data": data.decode("utf-8", errors="replace"),
    }


def _first_available_seq(runtime: _SessionRuntime) -> int:
    """返回 ring buffer 可用的最小 output seq。"""

    return _frame_seq(runtime.frames[0]) if runtime.frames else runtime.next_seq


def _frame_seq(frame: dict[str, object]) -> int:
    """读取内部 output frame 的已验证 seq。"""

    value = frame.get("seq")
    if not isinstance(value, int) or isinstance(value, bool):
        raise TerminalSessionError("terminal output frame has invalid sequence")
    return value


def _terminal_event(runtime: _SessionRuntime) -> dict[str, object]:
    """构造 session 终态事件。"""

    return {
        "protocol": "terminal-preview-v1",
        "type": "exit",
        "session_id": runtime.record.session_id,
        "generation": runtime.generation,
        "status": runtime.status,
        "exit_code": runtime.record.exit_code,
        "end_reason": runtime.record.end_reason,
    }
