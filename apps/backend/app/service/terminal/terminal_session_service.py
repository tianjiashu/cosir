"""本机 Terminal session 领域服务。

该服务拥有 session registry、PTY worker client、输出 seq/ring buffer 和只读预览订阅。
它不注册 Agent tool、不承载 Assistant UI 类型，也不把 PTY 输出写入 conversation snapshot。
ring buffer 只负责进程内 cursor 连续性；模型可见输出预算由统一的 ToolOutputBudget 负责。
"""

from __future__ import annotations

import base64
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from langchain_core.messages import SystemMessage

from app.config.constant import Constant
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
    TerminalWorkerBackpressureError,
    TerminalWorkerUnavailableError,
)
from app.service.terminal.session_status import (
    TerminalSessionStatusChange,
    TerminalSessionStatusObserverLike,
)
from app.service.terminal.shell_resolver import ShellResolver
from app.service.terminal.worker import (
    ProcessTerminalWorkerFactory,
    TerminalWorker,
    TerminalWorkerFactory,
    new_worker_instance_id,
)
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.utils.datetime_utils import utc_now

RunCancellationCheck = Callable[[], bool]


@dataclass(frozen=True)
class TerminalReadResult:
    """按 cursor 返回的非破坏性输出读取结果，不在此层施加模型输出预算。"""

    output: tuple[dict[str, object], ...]
    next_seq: int
    first_available_seq: int
    status: str

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

        self._queue: queue.Queue[dict[str, object]] = queue.Queue(
            maxsize=Constant.Terminal.MAX_PREVIEW_QUEUE_FRAMES
        )
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
                if len(self._pending) >= Constant.Terminal.MAX_PREVIEW_QUEUE_FRAMES:
                    self._mark_overflow_locked()
                    return
                self._pending.append(event)
                self._pending_bytes += _event_size(event)
                if self._pending_bytes > Constant.Terminal.MAX_PREVIEW_QUEUE_BYTES:
                    self._mark_overflow_locked()
                return
            try:
                event_bytes = _event_size(event)
                if self._queue_bytes + event_bytes > Constant.Terminal.MAX_PREVIEW_QUEUE_BYTES:
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
        # worker_instance_id is persisted diagnostic identity; generation is the
        # ephemeral callback fence for this in-process runtime attach.
        self.generation = f"gen_{uuid.uuid4().hex}"
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
    PTY 状态。session 元数据只作为 Run 级 workflow state 的可序列化投影进入 checkpoint；
    PTY 和 ring buffer 不跨 backend 重启恢复。
    """

    def __init__(
        self,
        worker_factory: TerminalWorkerFactory | None = None,
        shell_resolver: ShellResolver | None = None,
        *,
        max_active_sessions: int = Constant.Terminal.MAX_ACTIVE_SESSIONS,
        ring_buffer_bytes: int = Constant.Terminal.MAX_RING_BUFFER_BYTES,
        status_observer: TerminalSessionStatusObserverLike | None = None,
    ) -> None:
        """创建服务；不启动 worker。"""

        self._worker_factory = worker_factory or ProcessTerminalWorkerFactory()
        self._shell_resolver = shell_resolver or ShellResolver()
        self._max_active_sessions = max_active_sessions
        self._ring_buffer_bytes = ring_buffer_bytes
        self._status_observer = status_observer
        self._registry: dict[str, _SessionRuntime] = {}
        self._terminal_history: OrderedDict[str, _SessionRuntime] = OrderedDict()
        self._registry_lock = threading.RLock()
        self._closing_runs: set[int] = set()
        self._backpressure_notified_runs: set[tuple[int, int]] = set()
        self._write_operations: dict[tuple[str, str], tuple[bytes, TerminalReadResult | None]] = {}
        self._write_operations_condition = threading.Condition(threading.RLock())
        self._accepting = True
        self._sweeper_stop = threading.Event()
        self._sweeper_thread: threading.Thread | None = None

    def initialize(self) -> list[str]:
        """启动 sweeper；活终端不跨 backend 重启恢复。"""

        recovered: list[str] = []
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
        run_id: int,
    ) -> dict[str, object]:
        """创建一个由 Agent 驱动的交互式 shell session。"""

        self._ensure_accepting()
        with self._registry_lock:
            if run_id in self._closing_runs:
                raise TerminalSessionStateError("run is already closing")
        cwd_path = self._resolve_cwd(workspace_root, cwd)
        shell_spec = self._shell_resolver.resolve(shell)
        instance_id = new_worker_instance_id()
        session_id = f"term_{uuid.uuid4().hex}"
        now = utc_now()
        record = TerminalSessionRecord(
            session_id=session_id,
            task_id=task_id,
            workspace_id=workspace_id,
            run_id=run_id,
            initial_cwd=str(cwd_path),
            shell_kind=shell_spec.kind,
            shell_executable=shell_spec.executable,
            worker_instance_id=instance_id,
            worker_pid=None,
            status="starting",
            end_reason=None,
            exit_code=None,
            created_at=now,
            updated_at=now,
            last_activity_at=now,
            ended_at=None,
        )
        with self._registry_lock:
            if run_id in self._closing_runs:
                raise TerminalSessionStateError("run is already closing")
            self._ensure_capacity_locked()
            worker = self._worker_factory.create(instance_id)
            runtime = _SessionRuntime(record, worker)
            self._registry[session_id] = runtime
        try:
            worker.start(
                shell_spec,
                str(cwd_path),
                lambda event, generation=runtime.generation: self._on_worker_event(
                    session_id, generation, event
                ),
            )
            self._update_runtime(runtime, status="running", worker_pid=worker.pid)
            with self._registry_lock:
                stale = (
                    run_id in self._closing_runs
                    or self._registry.get(session_id) is not runtime
                )
            if stale:
                with suppress(Exception):
                    worker.close()
                raise TerminalSessionStateError("run is already closing")
            self._notify_status_changed(runtime)
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
        operation_id: str | None = None,
        workspace_id: int | None = None,
        after_seq: int | None = None,
        wait_ms: int = 0,
        is_cancelled: RunCancellationCheck | None = None,
    ) -> TerminalReadResult:
        """向 session 写入 Agent input，并可短暂等待 output 增量。"""

        if not data:
            raise TerminalSessionError("terminal input must not be empty")
        if len(data) > Constant.Terminal.MAX_AGENT_INPUT_BYTES:
            raise TerminalSessionError("terminal input exceeds 64 KiB")
        runtime = self._require_runtime(session_id, task_id, workspace_id=workspace_id)
        operation_key = (session_id, operation_id) if operation_id else None
        if operation_key is not None:
            with self._write_operations_condition:
                while True:
                    existing = self._write_operations.get(operation_key)
                    if existing is None:
                        self._write_operations[operation_key] = (data, None)
                        break
                    existing_data, existing_result = existing
                    if existing_data != data:
                        raise TerminalSessionError(
                            "terminal operation_id was reused with different input"
                        )
                    if existing_result is not None:
                        return existing_result
                    self._write_operations_condition.wait(timeout=0.05)
        with runtime.lock:
            try:
                self._ensure_mutable(runtime)
                worker = runtime.worker
                if worker is None:
                    raise TerminalSessionStateError("terminal session has no active worker")
                worker.write(data)
            except TerminalWorkerBackpressureError:
                self._defer_backpressure_message(runtime)
                if operation_key is not None:
                    self._forget_write_operation(operation_key)
                raise
            except TerminalWorkerUnavailableError:
                self._fail_runtime(runtime, "worker_unavailable")
                if operation_key is not None:
                    self._forget_write_operation(operation_key)
                raise
            except Exception:
                if operation_key is not None:
                    self._forget_write_operation(operation_key)
                raise
            self._touch(runtime)
        try:
            result = self.read(
                session_id,
                task_id=task_id,
                workspace_id=workspace_id,
                after_seq=after_seq,
                wait_ms=wait_ms,
                is_cancelled=is_cancelled,
            )
        except Exception:
            if operation_key is not None:
                self._forget_write_operation(operation_key)
            raise
        if operation_key is not None:
            with self._write_operations_condition:
                self._write_operations[operation_key] = (data, result)
                while len(self._write_operations) > Constant.Terminal.MAX_WRITE_OPERATION_CACHE:
                    self._write_operations.pop(next(iter(self._write_operations)))
                self._write_operations_condition.notify_all()
        return result

    def read(
        self,
        session_id: str,
        *,
        task_id: int,
        workspace_id: int | None = None,
        after_seq: int | None = None,
        wait_ms: int = 1000,
        is_cancelled: RunCancellationCheck | None = None,
    ) -> TerminalReadResult:
        """按 output cursor 非破坏性读取 session 输出。"""

        runtime = self._require_runtime(session_id, task_id, workspace_id=workspace_id)
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

    def signal(
        self,
        session_id: str,
        *,
        task_id: int,
        workspace_id: int | None = None,
        signal_name: str,
    ) -> dict[str, object]:
        """由 Agent 向 PTY 发送平台无关的 signal。"""

        if signal_name not in {"interrupt", "eof", "suspend"}:
            raise TerminalSessionError(f"unsupported terminal signal: {signal_name}")
        runtime = self._require_runtime(session_id, task_id, workspace_id=workspace_id)
        with runtime.lock:
            self._ensure_mutable(runtime)
            if runtime.worker is None:
                raise TerminalSessionStateError("terminal session has no active worker")
            required_capability = {
                "interrupt": "signal_interrupt",
                "eof": "signal_eof_canonical",
                "suspend": "signal_suspend",
            }[signal_name]
            if required_capability not in runtime.worker.capabilities:
                raise TerminalSessionStateError(
                    f"terminal signal is unsupported by worker: {signal_name}"
                )
            try:
                runtime.worker.signal(signal_name)
            except TerminalWorkerUnavailableError:
                self._fail_runtime(runtime, "worker_unavailable")
                raise
            self._touch(runtime)
        return self.snapshot(session_id, task_id=task_id, workspace_id=workspace_id)

    def close(
        self,
        session_id: str,
        *,
        task_id: int,
        workspace_id: int | None = None,
        reason: str = "agent_closed",
    ) -> dict[str, object]:
        """显式关闭 session；重复关闭只返回当前终态。"""

        runtime = self._require_runtime(session_id, task_id, workspace_id=workspace_id)
        with runtime.lock:
            if runtime.status in {"closed", "exited", "interrupted", "failed"}:
                return self.snapshot(session_id, task_id=task_id, workspace_id=workspace_id)
            worker = runtime.worker
            self._update_runtime(
                runtime,
                status="closed",
                end_reason=reason,
                ended_at=utc_now(),
            )
            runtime.condition.notify_all()
        close_error: Exception | None = None
        if worker is not None:
            try:
                worker.close()
            except Exception as exc:
                close_error = exc
                log.exception(
                    "terminal_session_close_worker_failed",
                    extra={
                        "msg": "Terminal Worker 显式关闭失败",
                        "data": {"session_id": session_id, "reason": reason},
                    },
                )
        self._publish_status(runtime)
        self._notify_status_changed(runtime)
        self._retire_runtime(runtime)
        if close_error is not None:
            raise close_error
        return self.snapshot(session_id, task_id=task_id, workspace_id=workspace_id)

    def snapshot(
        self,
        session_id: str,
        *,
        task_id: int,
        workspace_id: int | None = None,
    ) -> dict[str, object]:
        """读取 session 当前元数据和 cursor watermark。"""

        runtime = self._require_runtime(session_id, task_id, workspace_id=workspace_id)
        with runtime.lock:
            first_available = _first_available_seq(runtime)
            return {
                **runtime.record.to_dict(),
                "generation": runtime.generation,
                "first_available_seq": first_available,
                "next_seq": runtime.next_seq,
            }

    def get_status_change(
        self,
        session_id: str,
        *,
        task_id: int,
        run_id: int,
    ) -> TerminalSessionStatusChange | None:
        """按 task/run/session 精确查询当前进程持有的生命周期状态。

        该方法只服务 Transport 冷重建；未知 session 表示 PTY 不属于当前 backend
        生命周期，调用方可以据此收敛遗留的 ``running`` 展示数据。
        """

        with self._registry_lock:
            runtime = self._registry.get(session_id) or self._terminal_history.get(session_id)
        if runtime is None:
            return None
        with runtime.lock:
            record = runtime.record
            if record.task_id != task_id or record.run_id != run_id:
                return None
            return self._status_change_locked(runtime)

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
            }
            terminal_event = (
                _terminal_event(runtime)
                if runtime.status in {"exited", "interrupted", "failed", "closed"}
                else None
            )
            if terminal_event is None:
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
            self._terminal_history.clear()
        with self._write_operations_condition:
            self._write_operations.clear()
            self._write_operations_condition.notify_all()
        self._sweeper_stop.set()
        if (
            self._sweeper_thread is not None
            and self._sweeper_thread is not threading.current_thread()
        ):
            self._sweeper_thread.join(timeout=2)
        self._sweeper_thread = None

    def delete_task_sessions(self, task_ids: Iterable[int]) -> int:
        """在 Task 删除前关闭其仍存活的 terminal worker。

        终端元数据属于 checkpoint，不存在主库行需要删除；此方法仅释放当前进程资源。
        """

        ids = set(task_ids)
        if not ids:
            return 0
        closed = self._close_matching(
            lambda runtime: runtime.record.task_id in ids,
            reason="task_deleted",
            log_event="terminal_session_task_delete_worker_close_failed",
        )
        with self._registry_lock:
            self._backpressure_notified_runs = {
                key for key in self._backpressure_notified_runs if key[0] not in ids
            }
        return closed

    def begin_run(self, run_id: int) -> None:
        """允许一个新的执行轮次创建 terminal。

        同一 ``ConversationRun`` 业务 resume 时，旧轮次的 terminal 已经被关闭；
        resume 会在新的工具调用中创建新的 session，而不会复用旧 PTY。
        """

        with self._registry_lock:
            self._closing_runs.discard(run_id)

    def close_run_terminals(self, run_id: int, *, reason: str = "run_finished") -> int:
        """幂等强制关闭一个 Run 持有的全部 terminal worker。

        先设置 closing fence 并从 registry 脱离 session，阻止迟到的工具调用重新写入；
        然后发布终态、关闭 subscriber，并调用 worker 的 shutdown/terminate/kill 兜底。
        不修改 checkpoint；Run 状态由 ConversationRunModel 负责，checkpoint 只保存工具
        操作期间已投影的终端元数据。
        """

        with self._registry_lock:
            self._closing_runs.add(run_id)
        closed = self._close_matching(
            lambda runtime: runtime.record.run_id == run_id,
            reason=reason,
            log_event="terminal_session_run_close_worker_failed",
        )
        with self._registry_lock:
            self._backpressure_notified_runs = {
                key for key in self._backpressure_notified_runs if key[1] != run_id
            }
        return closed

    def _close_matching(
        self,
        predicate: Callable[[_SessionRuntime], bool],
        *,
        reason: str,
        log_event: str,
    ) -> int:
        """脱离并关闭满足条件的 session；不触碰 checkpoint 或主库。"""

        with self._registry_lock:
            runtimes = [runtime for runtime in self._registry.values() if predicate(runtime)]
            for runtime in runtimes:
                self._registry.pop(runtime.record.session_id, None)
        for runtime in runtimes:
            worker: TerminalWorker | None
            with runtime.lock:
                if runtime.status in {"starting", "running"}:
                    self._update_runtime(
                        runtime,
                        status="closed",
                        end_reason=reason,
                        ended_at=utc_now(),
                )
                worker = runtime.worker
                runtime.worker = None
                self._publish_status(runtime)
                runtime.subscribers.clear()
                runtime.condition.notify_all()
            self._forget_write_operations_for_session(runtime.record.session_id)
            if worker is not None:
                try:
                    worker.close()
                except Exception:
                    log.exception(
                        log_event,
                        extra={
                            "msg": "Terminal Worker 强制关闭失败",
                            "data": {
                                "session_id": runtime.record.session_id,
                                "run_id": runtime.record.run_id,
                                "reason": reason,
                            },
                        },
                    )
            self._notify_status_changed(runtime)
        return len(runtimes)

    def _on_worker_event(self, session_id: str, generation: str, event: object) -> None:
        """将 worker 事件投影到 session runtime。"""

        if not isinstance(event, dict):
            return
        with self._registry_lock:
            runtime = self._registry.get(session_id)
        if runtime is None:
            return
        if runtime.generation != generation:
            log.warning(
                "terminal_session_stale_worker_event",
                extra={
                    "msg": "丢弃旧 Terminal Worker callback",
                    "data": {"session_id": session_id},
                },
            )
            return
        event_type = event.get("type")
        if event_type == "handshake":
            return
        if event_type == "output":
            self._on_output(runtime, event)
        elif event_type == "error":
            self._on_worker_error(runtime, event)
        elif event_type == "exit":
            self._on_exit(runtime, event)

    def _forget_write_operation(self, operation_key: tuple[str, str]) -> None:
        """Remove a failed in-flight operation and wake a retrying caller."""

        with self._write_operations_condition:
            self._write_operations.pop(operation_key, None)
            self._write_operations_condition.notify_all()

    def _defer_backpressure_message(self, runtime: _SessionRuntime) -> None:
        """在输入队列背压时向该 Task 延迟注入一次可重试提示。"""

        run_key = (runtime.record.task_id, runtime.record.run_id)
        with self._registry_lock:
            if run_key in self._backpressure_notified_runs:
                return
            self._backpressure_notified_runs.add(run_key)
        try:
            task_runtime_spaces.get_or_create(runtime.record.task_id).defer_system_message(
                SystemMessage(
                    content=(
                        "The terminal input queue is temporarily full. Wait briefly for "
                        "the terminal worker to drain, then retry terminal_write."
                    ),
                    additional_kwargs={
                        "source": "terminal_input_backpressure",
                        "run_id": runtime.record.run_id,
                    },
                )
            )
        except Exception:
            with self._registry_lock:
                self._backpressure_notified_runs.discard(run_key)
            log.warning(
                "terminal_session_backpressure_message_defer_failed",
                extra={
                    "msg": "Terminal 输入队列背压提示注入失败",
                    "data": {
                        "session_id": runtime.record.session_id,
                        "task_id": runtime.record.task_id,
                        "run_id": runtime.record.run_id,
                    },
                },
            )

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
        if not data or len(data) > Constant.Terminal.MAX_WORKER_OUTPUT_CHUNK_BYTES:
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
        with runtime.lock:
            runtime.worker = None
        self._notify_status_changed(runtime)
        self._retire_runtime(runtime)

    def _on_worker_error(self, runtime: _SessionRuntime, event: dict[str, object]) -> None:
        """将 Worker 错误事件映射为失败或可记录的非致命运行态。"""

        code = event.get("code")
        if code in {
            "START_ALREADY_COMPLETE",
            "PTY_QUERY_RESPONSE_FAILED",
        }:
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
        with runtime.lock:
            runtime.worker = None
        self._notify_status_changed(runtime)
        self._retire_runtime(runtime)

    def _status_change_locked(self, runtime: _SessionRuntime) -> TerminalSessionStatusChange:
        """在持有 runtime lock 时复制一份安全的状态通知。"""

        record = runtime.record
        return TerminalSessionStatusChange(
            task_id=record.task_id,
            run_id=record.run_id,
            session_id=record.session_id,
            generation=runtime.generation,
            status=record.status,
            end_reason=record.end_reason,
            exit_code=record.exit_code,
        )

    def _notify_status_changed(self, runtime: _SessionRuntime) -> None:
        """把已确认的 session 状态交给 Transport 观察者。

        观察者是展示旁路；其异常不能阻断 PTY 收尾、worker 清理或模型工具结果。
        调用发生在 runtime lock 外，避免观察者回写快照时形成锁顺序反转。
        """

        observer = self._status_observer
        if observer is None:
            return
        with runtime.lock:
            change = self._status_change_locked(runtime)
        try:
            observer(change)
        except Exception:
            log.exception(
                "terminal_session_status_observer_failed",
                extra={
                    "msg": "Terminal session 状态回写 Transport 失败",
                    "data": {
                        "task_id": change.task_id,
                        "run_id": change.run_id,
                        "session_id": change.session_id,
                        "status": change.status,
                    },
                },
            )

    def _retire_runtime(self, runtime: _SessionRuntime) -> None:
        """从 active registry 脱离终态 runtime，并保留有限的只读查询历史。"""

        session_id = runtime.record.session_id
        self._forget_write_operations_for_session(session_id)
        with runtime.lock:
            runtime.worker = None
            runtime.subscribers.clear()
        with self._registry_lock:
            if self._registry.get(session_id) is runtime:
                self._registry.pop(session_id, None)
            self._terminal_history[session_id] = runtime
            self._terminal_history.move_to_end(session_id)
            while len(self._terminal_history) > Constant.Terminal.MAX_TERMINAL_HISTORY_SESSIONS:
                self._terminal_history.popitem(last=False)

    def _require_runtime(
        self,
        session_id: str,
        task_id: int,
        *,
        workspace_id: int | None = None,
    ) -> _SessionRuntime:
        """取得当前 backend 进程中的 active 或有限终态 runtime。"""

        with self._registry_lock:
            runtime = self._registry.get(session_id) or self._terminal_history.get(session_id)
            if runtime is not None:
                if runtime.record.task_id != task_id or (
                    workspace_id is not None and runtime.record.workspace_id != workspace_id
                ):
                    raise TerminalSessionOwnershipError("terminal session does not belong to task")
                return runtime
        raise TerminalSessionNotFoundError(session_id)

    def _forget_write_operations_for_session(self, session_id: str) -> None:
        """删除一个 session 的所有幂等写缓存，避免终态引用继续存活。"""

        with self._write_operations_condition:
            stale = [key for key in self._write_operations if key[0] == session_id]
            for key in stale:
                self._write_operations.pop(key, None)
            self._write_operations_condition.notify_all()

    @staticmethod
    def _resolve_cwd(workspace_root: str, cwd: str | None) -> Path:
        """解析并限制初始 cwd 到 workspace 内。

        参数:
            workspace_root: workspace 根目录。
            cwd: 初始工作目录；为 ``None`` 时使用 workspace 根。

        返回:
            解析后的绝对目录路径。

        异常:
            TerminalSessionError: 解析失败、越界或目标不是目录时抛出。

        副作用:
            无（只解析路径、读取目录元信息）。

        说明:
            显式传 ``allow_reserved=True``：终端是原始 shell 通道（用户在会话内可自由
            ``cd`` 到 ``.cosir``），故不把 ``.cosir`` 只读保留区叠加到终端 cwd 解析上，
            与「``.cosir`` 只读保护，终端除外」的产品约定一致。
        """

        resolver = PathResolver(workspace_root)
        resolved, error = resolver.resolve_within_workspace(cwd or ".", allow_reserved=True)
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
        for frame in runtime.frames:
            if after_seq is not None and _frame_seq(frame) <= after_seq:
                continue
            selected.append(frame)
        return TerminalReadResult(
            output=tuple(selected),
            next_seq=runtime.next_seq,
            first_available_seq=_first_available_seq(runtime),
            status=runtime.status,
        )

    def _touch(self, runtime: _SessionRuntime) -> None:
        """刷新进程内 activity 元数据；不写主库。"""

        now_datetime = utc_now()
        with runtime.lock:
            runtime.record = _replace_record(runtime.record, "last_activity_at", now_datetime)
            monotonic_now = time.monotonic()
            if monotonic_now - runtime.last_touch_monotonic < 1.0:
                return
            runtime.last_touch_monotonic = monotonic_now

    def _update_runtime(self, runtime: _SessionRuntime, **values: object) -> None:
        """更新进程内 record；checkpoint 投影由 workflow 工具节点完成。"""

        current = runtime.record
        for name, value in values.items():
            if value is not None and hasattr(current, name):
                current = _replace_record(current, name, value)
        runtime.record = current

    def _sweep_expired(self) -> None:
        """定期关闭超出生命周期或 idle timeout 的 session。"""

        while not self._sweeper_stop.wait(Constant.Terminal.SWEEPER_INTERVAL_SECONDS):
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
                        if lifetime >= Constant.Terminal.DEFAULT_MAX_LIFETIME_SECONDS
                        else "idle_timeout"
                        if idle >= Constant.Terminal.DEFAULT_IDLE_TIMEOUT_SECONDS
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
    "PTY_OUTPUT_READ_FAILED": "worker_pty_output_read_failed",
    "BACKEND_OUTPUT_FAILED": "worker_backend_output_failed",
    "PTY_CLEANUP_FAILED": "worker_pty_cleanup_failed",
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
