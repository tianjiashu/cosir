"""单会话执行协调；会话锁保护输入、观察、预览及唯一终态迁移。

service 仍拥有会话集合、Task 归属和外部状态通知。此对象不访问 registry，
终态迁移交出 worker 句柄，清理 IO 与外部通知由 service 在锁外完成。
"""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Final, Literal

from app.config.constant import Constant
from app.config.logging.logger import log
from app.tools.terminal_runtime.errors import (
    TerminalReadInProgressError,
    TerminalRepeatedReadError,
    TerminalSessionError,
    TerminalSessionStateError,
)
from app.tools.terminal_runtime.process_terminal_worker import ProcessTerminalWorker, WorkerEventCallback
from app.tools.terminal_runtime.shell_resolver import ShellSpec
from app.tools.terminal_runtime.terminal_observation import TerminalObservation
from app.tools.terminal_runtime.terminal_preview import (
    PreviewBuffer,
    TerminalPreviewAttachment,
    TerminalPreviewSubscription,
)
from app.tools.terminal_runtime.terminal_read_guard import TerminalReadGuard
from app.tools.terminal_runtime.terminal_session_status import TerminalSessionStatusChange

RunCancellationCheck = Callable[[], bool]


@dataclass(frozen=True)
class SessionTermination:
    """首次终态迁移的不可变通知与待释放句柄；重复迁移不再产生此结果。"""

    change: TerminalSessionStatusChange
    worker: ProcessTerminalWorker | None
    disposal_finished: threading.Event


class TerminalSessionRuntime:
    """单会话的状态与并发边界；不访问会话集合或外部状态观察者。"""

    def __init__(
        self,
        worker: ProcessTerminalWorker,
        *,
        session_id: str,
        task_id: int,
        workspace_id: int,
        created_by_run_id: int,
        initial_cwd: str,
        shell_kind: str,
        label: str | None,
        integration: str,
        buffer_limit: int,
    ) -> None:
        # 身份和用途在会话创建后不变；生命周期字段只在会话锁内更新。
        self.session_id: Final = session_id
        self.task_id: Final = task_id
        self.workspace_id: Final = workspace_id
        self.created_by_run_id: Final = created_by_run_id
        self.initial_cwd: Final = initial_cwd
        self.shell_kind: Final = shell_kind
        self.label: Final = label
        self._status = "starting"
        self._end_reason: str | None = None
        self._exit_code: int | None = None
        self._worker: ProcessTerminalWorker | None = worker
        self.generation = f"gen_{uuid.uuid4().hex}"
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._read_lock = threading.Lock()
        self._read_guard = TerminalReadGuard()
        # service 用此锁串行投递状态通知；不在持有状态锁时等待外部观察者。
        self.status_delivery_lock = threading.RLock()
        self._disposal_finished = threading.Event()
        self._preview = PreviewBuffer(session_id, self.generation, buffer_limit)
        self._observation = TerminalObservation(self.generation, buffer_limit, integration)
        self._capabilities: frozenset[str] = frozenset()
        self._accepted_writes: OrderedDict[str, bytes] = OrderedDict()

    @property
    def status(self) -> str:
        """集合筛选只读取单一状态；完整生命周期快照必须在会话锁内读取。"""
        return self._status

    def start(
        self, spec: ShellSpec, on_event: WorkerEventCallback
    ) -> TerminalSessionStatusChange | None:
        """与 finish 串行化，保证慢启动与 Task 删除交错时句柄不会脱管。"""
        with self._lock:
            worker = self._mutable_worker()
            worker.start(spec, self.initial_cwd, on_event)
            self._capabilities = worker.capabilities
            if self._observation.screen is None:
                self._observation.initialize_screen(worker.geometry)
            if self.status != "starting":
                return None
            self._status = "running"
            log.info(
                "terminal_worker_started",
                extra={
                    "msg": "终端 worker 已建立，会话与进程诊断信息已关联",
                    "data": {
                        "task_id": self.task_id,
                        "session_id": self.session_id,
                        "worker_instance_id": worker.instance_id,
                        "worker_pid": worker.pid,
                        "shell_executable": spec.executable,
                        "geometry": worker.geometry,
                        "screen_engine": "alacritty_terminal",
                    },
                },
            )
            return self.status_change()

    def wait_for_integration(self) -> None:
        deadline = time.monotonic() + 2.0
        with self._condition:
            while (
                self._observation.integration["status"] == "starting" and self.status == "running"
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._shell_event({"kind": "unavailable", "reason": "initialization_timeout"})
                    break
                self._condition.wait(remaining)

    def write(
        self,
        input: dict[str, str],
        operation_id: str,
        wait_ms: int,
        is_cancelled: RunCancellationCheck | None,
    ) -> dict[str, Any]:
        with self._lock:
            duplicate = self._accept_input(input, operation_id, is_cancelled)
        # 保持同一运行态引用；Task 删除也不会撤回已经接受的输入或丢失接受事实。
        return self._observe(
            wait_ms,
            is_cancelled,
            {"operation_id": operation_id, "accepted": True, "duplicate": duplicate},
        )

    def _accept_input(
        self, input: dict[str, str], operation_id: str, is_cancelled: RunCancellationCheck | None
    ) -> bool:
        """参数合法性由工具边界负责；接受缓存先于存活检查，保证退出后重试也不重发。"""
        fingerprint = sha256(
            json.dumps(input, sort_keys=True, ensure_ascii=False).encode()
        ).digest()
        if operation_id in self._accepted_writes:
            if self._accepted_writes[operation_id] != fingerprint:
                raise TerminalSessionError("terminal operation_id was reused with different input")
            return True
        if is_cancelled is not None and is_cancelled():
            raise TerminalSessionStateError("terminal input was not accepted: run cancelled")
        worker = self._mutable_worker()
        if input["type"] == "key" and f"input_{input['key']}" not in self._capabilities:
            raise TerminalSessionError(
                f"control key is not supported by this worker: {input['key']}"
            )
        worker.write(input)
        self._accepted_writes[operation_id] = fingerprint
        while len(self._accepted_writes) > Constant.Terminal.MAX_WRITE_OPERATION_CACHE:
            self._accepted_writes.popitem(last=False)
        return False

    def read(
        self,
        run_id: int,
        wait_ms: int,
        is_cancelled: RunCancellationCheck | None,
    ) -> dict[str, Any]:
        """同一会话只允许一个 read 等待，防止并发绕过节奏；不阻塞输入和 worker。"""
        if not self._read_lock.acquire(blocking=False):
            raise TerminalReadInProgressError(
                "A read is already waiting for this session; await its result."
            )
        try:
            return self._observe(wait_ms, is_cancelled, run_id=run_id)
        finally:
            self._read_lock.release()

    def _observe(
        self,
        wait_ms: int,
        is_cancelled: RunCancellationCheck | None,
        accepted_input: dict[str, Any] | None = None,
        *,
        run_id: int | None = None,
    ) -> dict[str, Any]:
        """等待固定时长后取最新快照；取消与会话结束提前唤醒，不把静默当完成。

        read 的时长下限由工具参数边界保证；write 使用独立的输入后等待时长。
        """
        deadline = time.monotonic() + wait_ms / 1000
        with self._condition:
            while True:
                if is_cancelled is not None and is_cancelled():
                    reason = "cancelled"
                    break
                if self.status not in {"starting", "running"}:
                    reason = "session_ended"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = "wait_elapsed"
                    break
                self._condition.wait(timeout=min(remaining, 0.05))
            observation = self._observation
            envelope = {
                "session_id": self.session_id,
                "generation": self.generation,
                "state": {
                    **observation.state(self.status),
                    "session_exit_code": self._exit_code,
                    "session_end_reason": self._end_reason,
                },
                "integration": observation.integration,
                "capabilities": sorted(self._capabilities),
                "return_reason": reason,
            }
            if accepted_input is not None:
                envelope["input"] = accepted_input
            # 预留重复观察提示与 handler 包装，通用预算不应二次截断结构化结果。
            result = observation.snapshot(envelope, Constant.Tools.MAX_OUTPUT_CHARS - 1024)
            if run_id is not None and reason != "cancelled":
                try:
                    warning = self._read_guard.observe(
                        run_id, (observation.information_version, self.status)
                    )
                except TerminalRepeatedReadError:
                    log.warning(
                        "terminal_read_repeated",
                        extra={
                            "msg": "连续观察无新信息，要求 Agent 调整等待或操作",
                            "data": {
                                "task_id": self.task_id,
                                "run_id": run_id,
                                "session_id": self.session_id,
                                "unchanged_reads": self._read_guard.repeats,
                            },
                        },
                    )
                    raise
                if warning is not None:
                    result["warning"] = warning
                    log.info(
                        "terminal_read_unchanged",
                        extra={
                            "msg": "终端观察无新信息，交付画面并提示重复次数",
                            "data": {
                                "task_id": self.task_id,
                                "run_id": run_id,
                                "session_id": self.session_id,
                                "unchanged_reads": warning["unchanged_reads"],
                            },
                        },
                    )
            return result

    def signal(self, signal_name: str) -> dict[str, object]:
        required = {
            "interrupt": "signal_interrupt",
            "eof": "signal_eof_canonical",
            "suspend": "signal_suspend",
        }[signal_name]
        with self._lock:
            worker = self._mutable_worker()
            if required not in self._capabilities:
                raise TerminalSessionStateError(
                    f"terminal signal is unsupported by worker: {signal_name}"
                )
        # signal 确认由同一 reader 接收；不能持锁阻止 reader 先处理输出或退出。
        worker.signal(signal_name)
        return self.snapshot()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "session_id": self.session_id,
                "task_id": self.task_id,
                "label": self.label,
                "initial_cwd": self.initial_cwd,
                "shell_kind": self.shell_kind,
                "status": self.status,
                "end_reason": self._end_reason,
                "exit_code": self._exit_code,
                "generation": self.generation,
                **self._preview.snapshot(),
                "geometry": self._observation.geometry()
                if self._observation.screen is not None
                else None,
                "integration": self._observation.integration.copy(),
                "state": self._observation.state(self.status),
                "capabilities": sorted(self._capabilities),
            }

    def status_change(self) -> TerminalSessionStatusChange:
        with self._lock:
            return TerminalSessionStatusChange(
                task_id=self.task_id,
                session_id=self.session_id,
                generation=self.generation,
                status=self.status,
                end_reason=self._end_reason,
                exit_code=self._exit_code,
            )

    def subscribe(self, after_seq: int | None) -> TerminalPreviewAttachment:
        with self._lock:
            return self._preview.attach(
                after_seq,
                task_id=self.task_id,
                status=self.status,
                geometry=self._observation.geometry(),
                exit_code=self._exit_code,
                end_reason=self._end_reason,
            )

    def unsubscribe(self, subscription: TerminalPreviewSubscription) -> None:
        with self._lock:
            self._preview.detach(subscription)

    def on_worker_event(self, event: Mapping[str, Any]) -> SessionTermination | None:
        """worker 边界的事件应用；只有首次终态迁移才返回待清理结果。"""
        kind = event.get("type")
        if kind == "output":
            return self._output(event)
        if kind == "screen":
            with self._condition:
                if self.status in {"starting", "running"}:
                    self._observation.update_screen(event["screen"], event["history"])
                    self._condition.notify_all()
        elif kind == "shell":
            with self._condition:
                if self.status in {"starting", "running"}:
                    self._shell_event(event["event"])
        elif kind == "exit":
            code = event.get("exit_code")
            return self.finish("exited", "shell_exited", code if isinstance(code, int) else None)
        elif kind == "error":
            code = event.get("code")
            if code in {"START_ALREADY_COMPLETE", "PTY_QUERY_RESPONSE_FAILED"}:
                log.warning(
                    "terminal_session_worker_nonfatal_error",
                    extra={
                        "msg": "Terminal Worker 报告非致命错误",
                        "data": {"session_id": self.session_id, "code": code},
                    },
                )
            else:
                reason = (
                    _FATAL_WORKER_ERROR_REASONS.get(code, "worker_failed")
                    if isinstance(code, str)
                    else "worker_failed"
                )
                return self.finish("failed", reason)
        return None

    def _shell_event(self, event: dict[str, Any]) -> None:
        projected = self._observation.shell_event(event)
        self._condition.notify_all()
        if projected is not None:
            log.info(
                "terminal_" + projected["type"],
                extra={
                    "msg": "终端 shell 观察",
                    "data": {
                        "task_id": self.task_id,
                        "session_id": self.session_id,
                        "generation": self.generation,
                        "command_id": projected.get("command_id"),
                        "reason": projected.get("reason"),
                    },
                },
            )

    def _output(self, event: Mapping[str, Any]) -> SessionTermination | None:
        encoded = event.get("data_base64")
        if not isinstance(encoded, str):
            return None
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            return self.finish("failed", "invalid_worker_output")
        if not data or len(data) > Constant.Terminal.MAX_WORKER_OUTPUT_CHUNK_BYTES:
            return self.finish("failed", "worker_output_chunk_too_large")
        with self._condition:
            if self.status in {"starting", "running"}:
                self._preview.append(encoded, len(data))
                self._condition.notify_all()
        return None

    def finish(
        self,
        status: Literal["closed", "exited", "failed"],
        reason: str,
        exit_code: int | None = None,
    ) -> SessionTermination | None:
        """原子确定终态、唤醒读取并交出唯一 worker 句柄；不执行外部 IO。"""
        with self._condition:
            if self.status not in {"starting", "running"}:
                return None
            active = self._observation.active
            self._observation.end_tracking("session_ended")
            if active is not None:
                log.info(
                    "terminal_tracking_ended",
                    extra={
                        "msg": "会话结束，未收到 shell 返回结果",
                        "data": {
                            "task_id": self.task_id,
                            "session_id": self.session_id,
                            "generation": self.generation,
                            "command_id": active,
                        },
                    },
                )
            self._end_reason = reason
            self._exit_code = exit_code
            # status 最后发布；锁外的集合筛选无需读取其他生命周期字段。
            self._status = status
            worker, self._worker = self._worker, None
            self._preview.finish(
                status=self.status, exit_code=self._exit_code, end_reason=self._end_reason
            )
            self._condition.notify_all()
            return SessionTermination(self.status_change(), worker, self._disposal_finished)

    def wait_for_disposal(self) -> None:
        """仅供批量清理等待已有句柄持有者；普通回调或重复 close 不等待自身收尾。"""
        self._disposal_finished.wait()

    def _mutable_worker(self) -> ProcessTerminalWorker:
        if self.status not in {"starting", "running"}:
            raise TerminalSessionStateError(f"terminal session is {self.status}")
        # worker 只在终态迁移时移交；活会话一定持有句柄。
        assert self._worker is not None
        return self._worker


_FATAL_WORKER_ERROR_REASONS = {
    "HEARTBEAT_TIMEOUT": "worker_heartbeat_timeout",
    "INVALID_INPUT": "worker_invalid_input",
    "PTY_WRITE_FAILED": "worker_pty_write_failed",
    "PTY_OUTPUT_READ_FAILED": "worker_pty_output_read_failed",
    "BACKEND_OUTPUT_FAILED": "worker_backend_output_failed",
    "PTY_CLEANUP_FAILED": "worker_pty_cleanup_failed",
    "PTY_WAIT_FAILED": "worker_pty_wait_failed",
}
