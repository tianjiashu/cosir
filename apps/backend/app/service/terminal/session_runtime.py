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
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any, Literal

from app.config.constant import Constant
from app.config.logging.logger import log
from app.models.terminal_session_record import TerminalSessionRecord
from app.service.terminal.errors import TerminalSessionError, TerminalSessionStateError
from app.service.terminal.observation import TerminalObservation
from app.service.terminal.terminal_output_sanitizer import TerminalOutputSanitizer
from app.service.terminal.preview import (
    PreviewBuffer,
    TerminalPreviewAttachment,
    TerminalPreviewSubscription,
)
from app.service.terminal.session_status import TerminalSessionStatusChange
from app.service.terminal.shell_resolver import ShellSpec
from app.service.terminal.process_terminal_worker import ProcessTerminalWorker, WorkerEventCallback
from app.utils.datetime_utils import utc_now

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
        record: TerminalSessionRecord,
        worker: ProcessTerminalWorker,
        *,
        integration: str,
        buffer_limit: int,
    ) -> None:
        self._record = record
        self._worker: ProcessTerminalWorker | None = worker
        self.generation = f"gen_{uuid.uuid4().hex}"
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        # service 用此锁串行投递状态通知；不在持有状态锁时等待外部观察者。
        self.status_delivery_lock = threading.RLock()
        self._disposal_finished = threading.Event()
        self._preview = PreviewBuffer(record.session_id, self.generation, buffer_limit)
        self._sanitizer = TerminalOutputSanitizer()
        self._observation = TerminalObservation(self.generation, buffer_limit, integration)
        self._capabilities: frozenset[str] = frozenset()
        self._accepted_writes: OrderedDict[str, bytes] = OrderedDict()

    @property
    def record(self) -> TerminalSessionRecord:
        return self._record

    @property
    def status(self) -> str:
        return self._record.status

    def start(
        self, spec: ShellSpec, on_event: WorkerEventCallback
    ) -> TerminalSessionStatusChange | None:
        """与 finish 串行化，保证慢启动与 Task 删除交错时句柄不会脱管。"""
        with self._lock:
            worker = self._mutable_worker()
            worker.start(spec, self.record.initial_cwd, on_event)
            self._capabilities = worker.capabilities
            if self.status != "starting":
                return None
            self._record = replace(self.record, status="running", worker_pid=worker.pid)
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
            cursor = self._observation.cursor()
            duplicate = self._accept_input(input, operation_id, is_cancelled)
        # 保持同一运行态引用；Task 删除也不会撤回已经接受的输入或丢失接受事实。
        return self._observe(
            cursor,
            {"until": "activity", "timeout_ms": wait_ms},
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
        self._touch()
        return False

    def read(
        self,
        cursor: str | None,
        wait: dict[str, Any] | None,
        is_cancelled: RunCancellationCheck | None,
    ) -> dict[str, Any]:
        return self._observe(cursor, wait, is_cancelled)

    def _observe(
        self,
        cursor: str | None,
        wait: dict[str, Any] | None,
        is_cancelled: RunCancellationCheck | None,
        accepted_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """等待与分页共用会话 condition；不通过 registry 重新定位已获得的运行态。"""
        wait = wait or {"until": "activity", "timeout_ms": 3000}
        deadline = time.monotonic() + wait["timeout_ms"] / 1000
        collect_until: float | None = None
        with self._condition:
            observation = self._observation
            seq, offset = observation.position(cursor)
            until = wait["until"]
            command_id = str(wait.get("command_id", ""))
            if until == "command_returned" and command_id not in observation.commands:
                raise TerminalSessionError("command is unknown or its retained result expired")
            if until == "shell_prompt" and observation.integration["status"] in {
                "off",
                "unavailable",
            }:
                raise TerminalSessionError("shell prompt observation is unavailable")
            while True:
                now = time.monotonic()
                reason = "timeout"
                condition = None
                if until == "command_returned":
                    condition = observation.commands.get(command_id)
                    if condition is not None:
                        reason = condition["type"]
                elif until == "shell_prompt" and observation.prompt_seq > seq:
                    reason = "shell_prompt"
                activity = observation.next_seq - 1 > seq or offset > 0
                if until == "activity" and activity:
                    if collect_until is None:
                        collect_until = min(deadline, now + 0.075)
                    if now >= collect_until:
                        reason = "activity"
                if self.status not in {"starting", "running"} and reason == "timeout":
                    reason = "session_ended"
                if is_cancelled is not None and is_cancelled():
                    reason = "cancelled"
                envelope = {
                    "session_id": self.record.session_id,
                    "generation": self.generation,
                    "state": {
                        **observation.state(self.status),
                        "session_exit_code": self.record.exit_code,
                        "session_end_reason": self.record.end_reason,
                    },
                    "integration": observation.integration,
                    "capabilities": sorted(self._capabilities),
                    "return_reason": reason,
                }
                if condition is not None:
                    envelope["condition"] = condition
                if accepted_input is not None:
                    envelope["input"] = accepted_input
                # 预算同源；预留 handler 的一行说明及编码包装，正文不会再被通用字符串截断。
                page = observation.page(cursor, envelope, Constant.Tools.MAX_OUTPUT_CHARS - 256)
                if page["gap"]:
                    log.warning(
                        "terminal_cursor_gap",
                        extra={
                            "msg": "Agent 观察窗口已淘汰部分内容",
                            "data": {
                                "task_id": self.record.task_id,
                                "session_id": self.record.session_id,
                                "generation": self.generation,
                            },
                        },
                    )
                if page["has_more"] or page["gap"]:
                    page["return_reason"] = "page_available" if reason == "timeout" else reason
                    return page
                if reason != "timeout" or now >= deadline:
                    return page
                remaining = deadline - now
                if collect_until is not None:
                    remaining = min(remaining, collect_until - now)
                self._condition.wait(timeout=min(remaining, 0.05))

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
        with self._lock:
            self._touch()
            return self.snapshot()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                **self.record.to_dict(),
                "generation": self.generation,
                **self._preview.snapshot(),
                "integration": self._observation.integration.copy(),
                "state": self._observation.state(self.status),
                "capabilities": sorted(self._capabilities),
                "next_cursor": self._observation.cursor(),
            }

    def status_change(self) -> TerminalSessionStatusChange:
        with self._lock:
            record = self.record
            return TerminalSessionStatusChange(
                task_id=record.task_id,
                session_id=record.session_id,
                generation=self.generation,
                status=record.status,
                end_reason=record.end_reason,
                exit_code=record.exit_code,
            )

    def subscribe(self, after_seq: int | None) -> TerminalPreviewAttachment:
        with self._lock:
            return self._preview.attach(
                self.record, after_seq, ended=self.status not in {"starting", "running"}
            )

    def unsubscribe(self, subscription: TerminalPreviewSubscription) -> None:
        with self._lock:
            self._preview.detach(subscription)

    def on_worker_event(self, event: Mapping[str, Any]) -> SessionTermination | None:
        """worker 边界的事件应用；只有首次终态迁移才返回待清理结果。"""
        kind = event.get("type")
        if kind == "output":
            return self._output(event)
        if kind == "shell":
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
                        "data": {"session_id": self.record.session_id, "code": code},
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
                        "task_id": self.record.task_id,
                        "session_id": self.record.session_id,
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
                self._observation.output(self._sanitizer.feed(data))
                self._preview.append(encoded, len(data))
                self._touch()
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
            self._observation.output(self._sanitizer.finish())
            active = self._observation.active
            self._observation.end_tracking("session_ended")
            if active is not None:
                log.info(
                    "terminal_tracking_ended",
                    extra={
                        "msg": "会话结束，未收到 shell 返回结果",
                        "data": {
                            "task_id": self.record.task_id,
                            "session_id": self.record.session_id,
                            "generation": self.generation,
                            "command_id": active,
                        },
                    },
                )
            now = utc_now()
            self._record = replace(
                self.record,
                status=status,
                end_reason=reason,
                exit_code=exit_code,
                ended_at=now,
            )
            worker, self._worker = self._worker, None
            self._preview.finish(self.record)
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

    def _touch(self) -> None:
        self._record = replace(self.record, last_activity_at=utc_now())


_FATAL_WORKER_ERROR_REASONS = {
    "HEARTBEAT_TIMEOUT": "worker_heartbeat_timeout",
    "INVALID_INPUT": "worker_invalid_input",
    "PTY_WRITE_FAILED": "worker_pty_write_failed",
    "PTY_OUTPUT_READ_FAILED": "worker_pty_output_read_failed",
    "BACKEND_OUTPUT_FAILED": "worker_backend_output_failed",
    "PTY_CLEANUP_FAILED": "worker_pty_cleanup_failed",
    "PTY_WAIT_FAILED": "worker_pty_wait_failed",
}
