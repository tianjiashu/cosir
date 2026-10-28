"""单会话执行协调：会话锁保护输入租约、观察、条件等待、交接、预览及唯一终态迁移。

service 拥有会话集合、Task 归属与容量；本对象不访问 registry。会话锁内只做内存操作：
外部副作用（快照变化通知、后台结论投递）经 service 注入的 ``schedule`` 交给 service 的单线程
副作用执行器，worker 句柄在终态迁移时交出、由 service 在锁外清理。因此 worker 读线程回调
本对象时只会短暂持锁，不做阻塞 IO。

并发约定：
- 等待都在 ``self._condition`` 上进行：worker 事件 ``notify_all`` 唤醒，
  ``WAIT_CANCEL_CHECK_SECONDS`` 时间片只用于复查 Run 取消信号；
- 输入租约同一时刻只有一个持有方（``agent`` / ``user`` / 空闲），用户的 Ctrl-C 例外：
  即使 Agent 持有也送达，并把租约转给用户；
- 需要 worker 回执的 signal 必须在锁外发送：回执由同一读线程接收，持锁发送会与读线程互等；
- worker 握手（最多数秒）不在会话锁内进行：启动期间快照投影、订阅与 WS 输入都要拿会话锁，
  其中 snapshot 刷新持有全局投影锁、WS 在事件循环里调用，长持会话锁会拖住所有 Task 与事件循环。
  握手与 worker 关闭由独立的启动互斥 ``_start_lock`` 串行化，``starting`` 期间输入被拒。
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
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Final, Literal

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.workflow.deferred.deferred_results import deferred_results
from app.tools.terminal_runtime.errors import (
    TerminalInputHeldByUserError,
    TerminalIntegrationRequiredError,
    TerminalSessionError,
    TerminalSessionStateError,
)
from app.tools.terminal_runtime.observation_render import (
    KeepMode,
    SessionFacts,
    TerminalObservationResult,
    background_payload,
    render_observation,
)
from app.tools.terminal_runtime.process_terminal_worker import (
    ProcessTerminalWorker,
    WorkerEventCallback,
)
from app.tools.terminal_runtime.shell_resolver import ShellSpec
from app.tools.terminal_runtime.terminal_input import (
    DEFAULT_WRITE_WAIT,
    TerminalWait,
    require_input_capability,
    summarize_input,
)
from app.tools.terminal_runtime.terminal_observation import TerminalObservation
from app.tools.terminal_runtime.terminal_stream import (
    TerminalStreamAttachment,
    TerminalStreamBuffer,
)
from app.tools.terminal_runtime.terminal_wait import WaitBaseline, WaitEvaluator

RunCancellationCheck = Callable[[], bool]
EffectScheduler = Callable[[Callable[[], None]], None]
LeaseHolder = Literal["agent", "user"]
UserInputResult = Literal["accepted", "rejected_agent_input", "session_ended"]

_ACTIVE = frozenset({"starting", "running"})
_CTRL_C = b"\x03"
_NONFATAL_WORKER_ERRORS = frozenset(
    {"START_ALREADY_COMPLETE", "PTY_QUERY_RESPONSE_FAILED", "input_rejected", "resize_rejected"}
)
_FATAL_WORKER_ERROR_REASONS = {
    "HEARTBEAT_TIMEOUT": "worker_heartbeat_timeout",
    "PTY_WRITE_FAILED": "worker_pty_write_failed",
    "PTY_OUTPUT_READ_FAILED": "worker_pty_output_read_failed",
    "BACKEND_OUTPUT_FAILED": "worker_backend_output_failed",
    "PTY_CLEANUP_FAILED": "worker_pty_cleanup_failed",
    "PTY_WAIT_FAILED": "worker_pty_wait_failed",
    # 后端读线程补发：worker 未发 exit 就断开（崩溃、协议违约或事件处理出错）。
    "WORKER_DISCONNECTED": "worker_disconnected",
}


@dataclass(frozen=True)
class SessionTermination:
    """首次终态迁移交出的待释放句柄；重复迁移不再产生此结果。"""

    worker: ProcessTerminalWorker | None
    disposal_finished: threading.Event
    status: str
    reason: str


@dataclass(frozen=True)
class _Handoff:
    token: str
    reason: str
    tool_call_id: str


@dataclass
class _ReadSignature:
    version: int
    status: str
    request: tuple[object, ...]


@dataclass
class _UserMarks:
    first: int
    last: int


class TerminalSessionRuntime:
    """单会话的状态与并发边界；不访问会话集合。"""

    def __init__(
        self,
        worker: ProcessTerminalWorker,
        *,
        session_id: str,
        task_id: int,
        created_by: Literal["agent", "user"],
        initial_cwd: str,
        shell_kind: str,
        label: str | None,
        integration: str,
        schedule: EffectScheduler,
        on_changed: Callable[[], None],
    ) -> None:
        # 身份和用途在会话创建后不变；生命周期字段只在会话锁内更新。
        self.session_id: Final = session_id
        self.task_id: Final = task_id
        self.created_by: Final = created_by
        self.initial_cwd: Final = initial_cwd
        self.shell_kind: Final = shell_kind
        self.label: Final = label
        self.created_at: Final = datetime.now(UTC)
        self.generation: Final = f"gen_{uuid.uuid4().hex}"
        self._schedule = schedule
        self._on_changed = on_changed
        self._status = "starting"
        self._end_reason: str | None = None
        self._exit_code: int | None = None
        self._worker: ProcessTerminalWorker | None = worker
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        # 只串行化「worker 握手」与「worker 关闭」，从不在会话锁内获取，避免与读线程互等。
        self._start_lock = threading.Lock()
        self._pending_geometry: tuple[int, int] | None = None
        self._disposal_finished = threading.Event()
        self._stream = TerminalStreamBuffer(
            session_id, self.generation, Constant.Terminal.MAX_RING_BUFFER_BYTES
        )
        self._observation = TerminalObservation(shell_kind, integration)
        self._capabilities: frozenset[str] = frozenset()
        self._accepted_writes: OrderedDict[str, bytes] = OrderedDict()
        self._lease: LeaseHolder | None = None
        self._user_last_input = 0.0
        self._lease_watch_running = False
        self._user_marks: _UserMarks | None = None
        self._handoff: _Handoff | None = None
        self._last_read: _ReadSignature | None = None

    # ------------------------------------------------------------------ 生命周期

    @property
    def status(self) -> str:
        """集合筛选只读取单一状态；完整快照必须在会话锁内读取。"""
        return self._status

    def start(self, spec: ShellSpec, on_event: WorkerEventCallback) -> None:
        """在会话锁外完成 worker 握手，成功后迁移为 running。

        握手期间会话可被并发结束（Task 删除、关闭）：``finish`` 照常在会话锁内迁移终态并交出句柄，
        句柄的关闭经 :meth:`dispose_worker` 等握手结束后再进行，慢启动与删除交错时句柄不会脱管，
        也不会在 ``worker.start`` 半途被关闭。握手失败的异常抛给 service 收尾。
        """
        with self._start_lock:
            with self._lock:
                worker = self._mutable_worker(allow_starting=True)
            worker.start(spec, self.initial_cwd, on_event)
            with self._condition:
                self._capabilities = worker.capabilities
                if self._status != "starting":
                    return
                buffer = self._observation.buffer
                if buffer.rows == 0:
                    # 握手期间已到的 screen 事件比握手尺寸新，不覆盖。
                    buffer.initialize(worker.geometry["rows"], worker.geometry["columns"])
                self._status = "running"
                geometry, self._pending_geometry = self._pending_geometry, None
            log.info(
                "terminal_worker_started",
                extra={
                    "msg": "终端 worker 已建立，会话与进程诊断信息已关联",
                    "data": {
                        "task_id": self.task_id,
                        "session_id": self.session_id,
                        "created_by": self.created_by,
                        "worker_instance_id": worker.instance_id,
                        "worker_pid": worker.pid,
                        "shell_executable": spec.executable,
                        "geometry": worker.geometry,
                    },
                },
            )
        if geometry is not None:
            # 启动期间收到的 resize 在 running 后补发；补发失败不影响启动结果，只记录。
            try:
                worker.resize(*geometry)
            except TerminalSessionError as exc:
                log.warning(
                    "terminal_pending_resize_failed",
                    extra={
                        "msg": "启动期间记下的终端尺寸补发失败",
                        "data": {"session_id": self.session_id, "code": exc.code},
                    },
                )
        self._on_changed()

    def dispose_worker(self, worker: ProcessTerminalWorker) -> None:
        """关闭终态迁移交出的 worker；先等进行中的握手结束，避免与 ``worker.start`` 并发关闭。"""
        with self._start_lock:
            worker.close()

    def wait_for_integration(self) -> None:
        """集成初始化超时记为 unavailable；迟到的真实 ready 事件仍可恢复能力。"""
        deadline = time.monotonic() + Constant.Terminal.INTEGRATION_READY_TIMEOUT_SECONDS
        timed_out = False
        with self._condition:
            while self._observation.integration_status == "starting" and self._status in _ACTIVE:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._observation.shell_event(
                        {"kind": "unavailable", "reason": "initialization_timeout"}
                    )
                    timed_out = True
                    break
                self._condition.wait(remaining)
        if timed_out:
            self._on_changed()

    def finish(
        self,
        status: Literal["closed", "exited", "failed"],
        reason: str,
        exit_code: int | None = None,
    ) -> SessionTermination | None:
        """原子确定终态、唤醒所有等待并交出唯一 worker 句柄；外部 IO 由 service 在锁外完成。"""
        with self._condition:
            if self._status not in _ACTIVE:
                return None
            self._observation.end_tracking("session_ended")
            self._end_reason = reason
            self._exit_code = exit_code
            self._status = status
            self._lease = None
            worker, self._worker = self._worker, None
            self._stream.finish()
            self._condition.notify_all()
            handoff, self._handoff = self._handoff, None
        log.info(
            "terminal_session_ended",
            extra={
                "msg": "终端会话进入终态",
                "data": {
                    "task_id": self.task_id,
                    "session_id": self.session_id,
                    "status": status,
                    "reason": reason,
                    "exit_code": exit_code,
                },
            },
        )
        if handoff is not None:
            self._schedule(lambda: self._deliver_handoff(handoff, "session_ended"))
        return SessionTermination(worker, self._disposal_finished, status, reason)

    def wait_for_disposal(self) -> None:
        """仅供批量清理等待已有句柄持有者；普通回调或重复 close 不等待自身收尾。"""
        self._disposal_finished.wait()

    def _mutable_worker(self, *, allow_starting: bool = False) -> ProcessTerminalWorker:
        """持锁调用：取活会话的 worker；``starting`` 时握手未完成、worker 尚不接受输入。"""
        if self._status not in _ACTIVE or (self._status == "starting" and not allow_starting):
            raise TerminalSessionStateError(f"terminal session is {self._status}")
        # worker 只在终态迁移时移交；活会话一定持有句柄。
        return self._worker  # type: ignore[return-value]

    # ------------------------------------------------------------------ worker 事件

    def on_worker_event(self, event: Mapping[str, Any]) -> SessionTermination | None:
        """worker 读线程内调用：只做内存更新与唤醒；只有首次终态迁移返回待清理结果。"""
        kind = event.get("type")
        if kind == "output":
            return self._output(event)
        if kind == "screen":
            with self._condition:
                if self._status in _ACTIVE:
                    if self._observation.update_screen(event["screen"], event["history"]):
                        buffer = self._observation.buffer
                        self._stream.publish("geometry", rows=buffer.rows, columns=buffer.columns)
                    self._condition.notify_all()
        elif kind == "shell":
            self._shell_event(event["event"])
        elif kind == "exit":
            code = event.get("exit_code")
            return self.finish("exited", "shell_exited", code if isinstance(code, int) else None)
        elif kind == "error":
            code = event.get("code")
            if code in _NONFATAL_WORKER_ERRORS:
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
        with self._condition:
            if self._status not in _ACTIVE:
                return
            kind = self._observation.shell_event(event)
            self._condition.notify_all()
        if kind is None:
            return
        log.info(
            "terminal_shell_" + kind,
            extra={
                "msg": "终端 shell 集成事件",
                "data": {
                    "task_id": self.task_id,
                    "session_id": self.session_id,
                    "command_number": self._observation.command_counter,
                    "reason": event.get("reason"),
                },
            },
        )
        # 任一集成事件都可能改变 integration 或 commandRunning（prompt 也会收敛未闭合的命令）。
        self._on_changed()

    def _output(self, event: Mapping[str, Any]) -> SessionTermination | None:
        # 缺失或非字符串的 ``data_base64`` 与解码失败同属 worker 协议违约，一律收敛会话。
        encoded = event.get("data_base64")
        if not isinstance(encoded, str):
            return self.finish("failed", "invalid_worker_output")
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            return self.finish("failed", "invalid_worker_output")
        if not data or len(data) > Constant.Terminal.MAX_WORKER_OUTPUT_CHUNK_BYTES:
            return self.finish("failed", "worker_output_chunk_too_large")
        with self._condition:
            if self._status in _ACTIVE:
                self._stream.append(encoded, len(data))
                self._observation.output_arrived()
                self._condition.notify_all()
        return None

    # ------------------------------------------------------------------ Agent 输入

    def write(
        self,
        input: Mapping[str, Any],
        operation_id: str,
        wait: TerminalWait | None,
        *,
        run_id: int | None,
        tool_call_id: str,
        is_cancelled: RunCancellationCheck,
    ) -> TerminalObservationResult:
        """投递一次 Agent 输入并按条件等待；输入接受后的取消仍返回接受事实，避免模型重发。"""
        kind = input["type"]
        summary = summarize_input(input)
        if kind == "handoff":
            return self._start_handoff(str(input["reason"]), run_id, tool_call_id, summary)
        spec = wait or DEFAULT_WRITE_WAIT
        fingerprint = sha256(
            json.dumps(dict(input), sort_keys=True, ensure_ascii=False).encode()
        ).digest()
        with self._condition:
            self._require_wait_supported(spec)
            cached = self._accepted_writes.get(operation_id)
            if cached is not None:
                if cached != fingerprint:
                    raise TerminalSessionError(
                        "terminal operation_id was reused with different input"
                    )
                return self._duplicate_result(kind, summary)
            if is_cancelled():
                raise TerminalSessionStateError("terminal input was not accepted: run cancelled")
            worker = self._mutable_worker()
            require_input_capability(input, self._capabilities)
            self._acquire_agent_lease(is_cancelled)
            baseline = WaitBaseline.of(self._observation, after_line_input=kind == "line")
            if kind in {"line", "keys"}:
                self._stream.publish("input_mark", by="agent")
        try:
            self._deliver_agent_input(worker, input)
            with self._condition:
                self._accepted_writes[operation_id] = fingerprint
                while len(self._accepted_writes) > Constant.Terminal.MAX_WRITE_OPERATION_CACHE:
                    self._accepted_writes.popitem(last=False)
                self._last_read = None
                log.info(
                    "terminal_agent_input_accepted",
                    extra={
                        "msg": "Agent 终端输入已投递",
                        "data": {
                            "task_id": self.task_id,
                            "run_id": run_id,
                            "session_id": self.session_id,
                            "operation_id": operation_id,
                            "input_type": kind,
                            "wait_until": spec.until,
                            "timeout_ms": spec.effective_timeout_ms,
                        },
                    },
                )
                if spec.effective_timeout_ms > Constant.Terminal.WAIT_FOREGROUND_MAX_MS:
                    self._release_agent_lease()
                    return self._start_background_wait(
                        spec, baseline, run_id, tool_call_id, "both", kind, summary
                    )
                evaluator = WaitEvaluator(spec, baseline)
                reason = self._wait_foreground(
                    evaluator, is_cancelled, run_id=run_id, tool_call_id=tool_call_id
                )
                if reason is None:
                    return self._detached_result(baseline.line, "both", kind, summary, spec)
                return self._build_result(
                    baseline.line,
                    self._observation.buffer.last_line,
                    "both",
                    wait_result=evaluator.result(reason),
                    input_type=kind,
                    input_summary=summary,
                )
        finally:
            with self._condition:
                self._release_agent_lease()

    def _deliver_agent_input(self, worker: ProcessTerminalWorker, input: Mapping[str, Any]) -> None:
        """锁外投递：line / keys 只入队；signal 等待 worker 回执，持锁会与读线程互等。"""
        kind = input["type"]
        if kind == "signal":
            worker.signal(str(input["signal"]))
        elif kind == "line":
            worker.write({"type": "line", "text": input["text"]})
        else:
            worker.write({"type": "keys", "keys": list(input["keys"])})

    def _require_wait_supported(self, spec: TerminalWait) -> None:
        if spec.until == "command_returned" and self._observation.integration_status != "ready":
            raise TerminalIntegrationRequiredError(
                "wait until=command_returned needs shell integration, which is "
                f"{self._observation.integration_status} for this session; "
                "use until=output_quiet or until=match instead"
            )

    def _duplicate_result(self, kind: str, summary: str) -> TerminalObservationResult:
        buffer = self._observation.buffer
        return self._build_result(
            self._default_tail_start(),
            buffer.last_line,
            "tail",
            note="duplicate operation_id: input was already accepted and was not resent",
            input_type=kind,
            input_summary=summary,
        )

    # ------------------------------------------------------------------ Agent 读取

    def read(
        self,
        *,
        start_line: int | None,
        limit: int | None,
        tail: int | None,
        wait: TerminalWait | None,
        run_id: int | None,
        tool_call_id: str,
        is_cancelled: RunCancellationCheck,
    ) -> TerminalObservationResult:
        """按行读取；带 wait 时先按条件等待再读。无变化且无等待的重复读取只给一行提示。"""
        with self._condition:
            wait_result: dict[str, object] | None = None
            if wait is not None:
                self._require_wait_supported(wait)
                baseline = WaitBaseline.of(self._observation, after_line_input=False)
                # 空闲判定先于前台 / 后台分流：没有执行中的命令就没有可等的返回，长超时也不挂起。
                if wait.until == "command_returned" and baseline.active is None:
                    wait_result = {"until": wait.until, "end_reason": "idle"}
                elif wait.effective_timeout_ms > Constant.Terminal.WAIT_FOREGROUND_MAX_MS:
                    return self._start_background_wait(
                        wait, baseline, run_id, tool_call_id, "tail", None, None
                    )
                else:
                    evaluator = WaitEvaluator(wait, baseline)
                    reason = self._wait_foreground(
                        evaluator, is_cancelled, run_id=run_id, tool_call_id=tool_call_id
                    )
                    if reason is None:
                        return self._detached_result(
                            self._default_tail_start(), "tail", None, None, wait
                        )
                    wait_result = evaluator.result(reason)
            buffer = self._observation.buffer
            mode: KeepMode
            if start_line is not None:
                count = min(limit or Constant.Terminal.READ_MAX_LIMIT,
                            Constant.Terminal.READ_MAX_LIMIT)
                first, last, mode = start_line, start_line + count - 1, "head"
            else:
                count = min(tail or limit or Constant.Terminal.READ_DEFAULT_TAIL,
                            Constant.Terminal.READ_MAX_LIMIT)
                first, last, mode = buffer.last_line - count + 1, buffer.last_line, "tail"
            signature = _ReadSignature(
                self._observation.information_version, self._status, (start_line, limit, tail)
            )
            touches_live = last >= buffer.live_from_line
            if (
                wait is None
                and touches_live
                and self._last_read == signature
                and self._user_marks is None
            ):
                return TerminalObservationResult(
                    session_id=self.session_id,
                    status=self._status,
                    text=(
                        f"session {self.session_id}: no change since your last read "
                        f"(cursor line {buffer.cursor_line}). Use wait to block until "
                        "something happens instead of reading again."
                    ),
                )
            self._last_read = signature if wait is None else None
            return self._build_result(first, last, mode, wait_result=wait_result)

    def _wait_locked(self, evaluator: WaitEvaluator, is_cancelled: RunCancellationCheck) -> str:
        """持 ``self._condition`` 调用；worker 事件唤醒判定，时间片只为复查取消。"""
        while True:
            if is_cancelled():
                return "cancelled"
            if self._status not in _ACTIVE:
                return "session_ended"
            now = time.monotonic()
            reason, due = evaluator.check(self._observation, now)
            if reason is not None:
                return reason
            self._condition.wait(
                min(max(due - now, 0.0), Constant.Terminal.WAIT_CANCEL_CHECK_SECONDS)
            )

    # ------------------------------------------------------------------ 前台等待

    def _wait_foreground(
        self,
        evaluator: WaitEvaluator,
        is_cancelled: RunCancellationCheck,
        *,
        run_id: int | None,
        tool_call_id: str,
    ) -> str | None:
        """持锁调用：前台等待，同时登记为可撤销、可转到后台的前台条目；返回结束原因。

        返回 ``None`` 表示等待已转到后台（用户在看板或卡片上转走），调用方返回「已转后台」占位。
        转后台与正常结束的先后由 ``deferred_results.settle_foreground`` 在登记表锁内裁决：转后台
        先到而条件已满足时，已拿到的结论立即按后台回投；条件未满足时由后台线程接着等同一个评估器。
        用户在看板取消前台条目时，等待以 ``cancelled`` 结束、作为工具结果返回（前台没有占位行）。
        """
        cancelled, detached = threading.Event(), threading.Event()
        deferred_results.register(
            tool_call_id,
            task_id=self.task_id,
            run_id=run_id,
            kind="terminal_wait",
            label=f"terminal_wait:{self.session_id}",
            mode="foreground",
            on_cancel=lambda: self._cancel_background(cancelled),
            on_detach=lambda: self._cancel_background(detached),
        )
        reason = self._wait_locked(
            evaluator, lambda: is_cancelled() or cancelled.is_set() or detached.is_set()
        )
        outcome = deferred_results.settle_foreground(tool_call_id)
        if outcome == "settled":
            return reason
        if outcome == "gone":
            return "cancelled"
        if detached.is_set() and reason == "cancelled" and not is_cancelled():
            # 被转走时条件还没满足：后台线程接着等，撤销仍由同一个 ``cancelled`` 标记生效。
            threading.Thread(
                target=self._run_background_wait,
                args=(tool_call_id, cancelled, evaluator),
                name=f"terminal-wait-{self.session_id[-8:]}",
                daemon=True,
            ).start()
        else:
            # 条件恰好满足时被转走：结论已在手，按后台回投（写库放到锁外的线程）。
            payload = self._background_payload(evaluator.result(reason))
            threading.Thread(
                target=deferred_results.complete,
                args=(tool_call_id, payload),
                name=f"terminal-wait-{self.session_id[-8:]}",
                daemon=True,
            ).start()
        return None

    def _detached_result(
        self,
        first: int,
        mode: KeepMode,
        input_type: str | None,
        input_summary: str | None,
        spec: TerminalWait,
    ) -> TerminalObservationResult:
        """持锁调用：「已转后台」占位。不挂起：用户把等待转走是为了让 Agent 先做别的。"""
        result = self._build_result(
            first,
            self._observation.buffer.last_line,
            mode,
            note=(
                f"the user moved this wait for {spec.until} to the background; keep working, and "
                "its result will arrive as a <background_result> message for this tool call "
                "(use wait_messages if you need to pause for it)"
            ),
            input_type=input_type,
            input_summary=input_summary,
            wait_result={"until": spec.until, "end_reason": "background"},
        )
        return replace(result, deferred=True)

    # ------------------------------------------------------------------ 后台等待

    def _start_background_wait(
        self,
        spec: TerminalWait,
        baseline: WaitBaseline,
        run_id: int | None,
        tool_call_id: str,
        mode: KeepMode,
        input_type: str | None,
        input_summary: str | None,
    ) -> TerminalObservationResult:
        """持锁调用：以本次调用登记延迟结果、启动等待线程，立即返回当前观察并声明挂起。

        等待线程不是 PTY 读线程，条件满足或超时后在该线程内经 ``deferred_results.complete`` 投递；
        Run 停止时 ``deferred_results.drop_task`` 调用撤销回调，线程被唤醒后不投递。
        """
        # 撤销标记：Run 停止时由 ``deferred_results`` 的撤销回调置位。
        cancelled = threading.Event()
        deferred_results.register(
            tool_call_id,
            task_id=self.task_id,
            run_id=run_id,
            kind="terminal_wait",
            label=f"terminal_wait:{self.session_id}",
            on_cancel=lambda: self._cancel_background(cancelled),
        )
        evaluator = WaitEvaluator(spec, baseline)
        threading.Thread(
            target=self._run_background_wait,
            args=(tool_call_id, cancelled, evaluator),
            name=f"terminal-wait-{self.session_id[-8:]}",
            daemon=True,
        ).start()
        buffer = self._observation.buffer
        first = baseline.line if mode == "both" else self._default_tail_start()
        note = (
            f"waiting in the background for {spec.until} "
            f"(timeout {spec.effective_timeout_ms // 1000}s); you are paused now and will be "
            "resumed by a <background_result> message from terminal_wait"
        )
        result = self._build_result(
            first,
            buffer.last_line,
            mode,
            note=note,
            input_type=input_type,
            input_summary=input_summary,
            wait_result={"until": spec.until, "end_reason": "background"},
        )
        return replace(result, suspended=True, deferred=True)

    def _cancel_background(self, cancelled: threading.Event) -> None:
        with self._condition:
            cancelled.set()
            self._condition.notify_all()

    def _run_background_wait(
        self, tool_call_id: str, cancelled: threading.Event, evaluator: WaitEvaluator
    ) -> None:
        with self._condition:
            reason = self._wait_locked(evaluator, cancelled.is_set)
            payload = self._background_payload(evaluator.result(reason))
        if reason == "cancelled":
            return
        deferred_results.complete(tool_call_id, payload)

    def _background_payload(self, wait_result: dict[str, object]) -> dict[str, object]:
        """持锁调用：后台结论给模型的 JSON（见 ``observation_render.background_payload``）。"""
        return background_payload(self._facts(), self._observation, wait_result)

    # ------------------------------------------------------------------ 交接

    def _start_handoff(
        self, reason: str, run_id: int | None, tool_call_id: str, summary: str
    ) -> TerminalObservationResult:
        with self._condition:
            self._mutable_worker()
            if self._handoff is not None:
                raise TerminalSessionStateError("a handoff is already active for this session")
            token = uuid.uuid4().hex
            deferred_results.register(
                tool_call_id,
                task_id=self.task_id,
                run_id=run_id,
                kind="terminal_handoff",
                label=f"terminal_handoff:{self.session_id}",
                on_cancel=lambda: self._cancel_handoff(token),
            )
            self._handoff = _Handoff(token=token, reason=reason, tool_call_id=tool_call_id)
            buffer = self._observation.buffer
            result = self._build_result(
                self._default_tail_start(),
                buffer.last_line,
                "tail",
                note=(
                    "input handed off to the user; you are paused now and will be resumed by a "
                    "<background_result> message from terminal_handoff when the user is done"
                ),
                input_type="handoff",
                input_summary=summary,
            )
        log.info(
            "terminal_handoff_started",
            extra={
                "msg": "Agent 把终端输入权交给用户",
                "data": {"task_id": self.task_id, "run_id": run_id, "session_id": self.session_id},
            },
        )
        self._on_changed()
        return replace(result, suspended=True, deferred=True)

    def _cancel_handoff(self, token: str) -> None:
        """Run 取消撤销交接：只清提示，终端本身与用户输入不受影响。"""
        with self._condition:
            if self._handoff is None or self._handoff.token != token:
                return
            self._handoff = None
        self._on_changed()

    def complete_handoff(self) -> None:
        """用户点「交还」：清除交接状态，并经副作用执行器投递结论。

        交接只由交还、Run 取消（撤销回调）或会话结束收尾，不跟随输入租约：用户输入密码等操作时
        停顿几秒很常见，租约空闲释放若结束交接，会在用户还没完成时把 Agent 唤醒。
        """
        end_reason = "user_completed"
        with self._condition:
            handoff, self._handoff = self._handoff, None
            if handoff is None:
                return
        self._on_changed()
        self._schedule(lambda: self._deliver_handoff(handoff, end_reason))

    def _deliver_handoff(self, handoff: _Handoff, end_reason: str) -> None:
        """副作用线程内调用；交接已被撤销或后端正在关闭时 ``deliver`` 不投递。"""
        with self._condition:
            payload = self._background_payload({"end_reason": end_reason})
        payload["handoff_reason"] = handoff.reason
        delivered = deferred_results.complete(handoff.tool_call_id, payload)
        outcome = "，已投递结论" if delivered else "，结论未投递（已撤销或关闭）"
        log.info(
            "terminal_handoff_completed",
            extra={
                "msg": "终端交接结束" + outcome,
                "data": {
                    "task_id": self.task_id,
                    "session_id": self.session_id,
                    "end_reason": end_reason,
                    "delivered": delivered,
                },
            },
        )

    # ------------------------------------------------------------------ 输入租约

    def _acquire_agent_lease(self, is_cancelled: RunCancellationCheck) -> None:
        """持锁调用：等用户租约释放最多 ``AGENT_LEASE_WAIT_SECONDS``，超时报可重试错误。"""
        deadline = time.monotonic() + Constant.Terminal.AGENT_LEASE_WAIT_SECONDS
        waited = False
        while self._lease is not None:
            waited = True
            remaining = deadline - time.monotonic()
            if remaining <= 0 or is_cancelled() or self._status not in _ACTIVE:
                holder = self._lease
                log.info(
                    "terminal_agent_lease_timeout",
                    extra={
                        "msg": "Agent 等待终端输入租约超时",
                        "data": {"session_id": self.session_id, "holder": holder},
                    },
                )
                if self._status not in _ACTIVE:
                    raise TerminalSessionStateError(f"terminal session is {self._status}")
                if is_cancelled():
                    raise TerminalSessionStateError(
                        "terminal input was not accepted: run cancelled"
                    )
                if holder == "user":
                    raise TerminalInputHeldByUserError(
                        "the user is typing in this terminal; retry after they finish "
                        "(input is released after a few idle seconds)"
                    )
                raise TerminalSessionStateError("another agent input is still in progress")
            self._condition.wait(min(remaining, Constant.Terminal.WAIT_CANCEL_CHECK_SECONDS))
        if waited:
            log.info(
                "terminal_agent_lease_acquired_after_wait",
                extra={
                    "msg": "用户租约释放后 Agent 取得输入",
                    "data": {"session_id": self.session_id},
                },
            )
        self._set_lease("agent")

    def _release_agent_lease(self) -> None:
        if self._lease == "agent":
            self._set_lease(None)

    def _set_lease(self, holder: LeaseHolder | None) -> None:
        """持锁调用：租约变化经快照投影给前端（唯一事实源，WS 不另推）；等待者被唤醒复查。"""
        if self._lease == holder:
            return
        self._lease = holder
        self._condition.notify_all()
        self._on_changed()

    def user_input(self, data: bytes) -> UserInputResult:
        """用户原始按键：可在事件循环里直接调用，只做内存更新与非阻塞入队。

        先入队、成功后才改租约与输入标注：背压（``TerminalWorkerBackpressureError``）或 worker
        不可用时按键没有送达，状态必须保持原样。入队非阻塞、不与读线程交互，可在锁内进行。
        ``starting`` 时 worker 尚未握手，抛 ``TerminalSessionStateError``。
        """
        with self._condition:
            if self._status not in _ACTIVE:
                return "session_ended"
            if self._lease == "agent" and data != _CTRL_C:
                return "rejected_agent_input"
            worker = self._mutable_worker()
            worker.write({"type": "raw", "data_base64": base64.b64encode(data).decode("ascii")})
            cursor = self._observation.buffer.cursor_line
            if self._user_marks is None:
                self._user_marks = _UserMarks(cursor, cursor)
            else:
                self._user_marks.first = min(self._user_marks.first, cursor)
                self._user_marks.last = max(self._user_marks.last, cursor)
            self._user_last_input = time.monotonic()
            # Ctrl-C 在 Agent 持有时也送达，并把租约转给用户：人必须始终能停下失控命令。
            self._set_lease("user")
            if not self._lease_watch_running:
                self._lease_watch_running = True
                threading.Thread(
                    target=self._watch_user_lease,
                    name=f"terminal-lease-{self.session_id[-8:]}",
                    daemon=True,
                ).start()
        return "accepted"

    def _watch_user_lease(self) -> None:
        """用户租约到期释放：空闲满时长且 shell 不在执行中（无集成时只看空闲）。"""
        released = False
        with self._condition:
            try:
                while self._lease == "user" and self._status in _ACTIVE:
                    due = self._user_last_input + Constant.Terminal.USER_LEASE_IDLE_SECONDS
                    now = time.monotonic()
                    if now >= due and self._observation.phase != "executing":
                        self._set_lease(None)
                        released = True
                        break
                    # executing 时等 shell 事件唤醒（命令返回），时间片只是兜底复查。
                    self._condition.wait(
                        due - now if now < due else Constant.Terminal.WAIT_CANCEL_CHECK_SECONDS * 4
                    )
            finally:
                self._lease_watch_running = False
        if released:
            log.info(
                "terminal_user_lease_released",
                extra={
                    "msg": "用户输入空闲，释放终端租约",
                    "data": {"session_id": self.session_id},
                },
            )

    def release_user_input(self) -> None:
        """用户主动释放输入租约；不结束交接（见 :meth:`complete_handoff`）。"""
        with self._condition:
            if self._lease != "user":
                return
            self._set_lease(None)

    def resize(self, rows: int, columns: int) -> None:
        """``starting`` 时只记下最新尺寸，握手完成后由 :meth:`start` 补发。"""
        with self._condition:
            if self._status == "starting":
                self._pending_geometry = (rows, columns)
                return
            worker = self._mutable_worker()
        worker.resize(rows, columns)

    # ------------------------------------------------------------------ 结果渲染

    def _default_tail_start(self) -> int:
        """持锁调用：默认尾部窗口（``READ_DEFAULT_TAIL`` 行）的首行。

        下限由 ``_build_result`` 收拢。
        """
        return self._observation.buffer.last_line - Constant.Terminal.READ_DEFAULT_TAIL + 1

    def _build_result(
        self,
        first: int,
        last: int,
        mode: KeepMode,
        *,
        wait_result: dict[str, object] | None = None,
        note: str | None = None,
        input_type: str | None = None,
        input_summary: str | None = None,
    ) -> TerminalObservationResult:
        """持锁调用：渲染观察文本；用户输入标注在交付后清空。"""
        result = render_observation(
            self._facts(),
            self._observation,
            first,
            last,
            mode,
            wait_result=wait_result,
            note=note,
            input_type=input_type,
            input_summary=input_summary,
        )
        self._user_marks = None
        return result

    def _facts(self) -> SessionFacts:
        """持锁调用：渲染所需的会话侧事实。"""
        marks = self._user_marks
        return SessionFacts(
            session_id=self.session_id,
            shell_kind=self.shell_kind,
            status=self._status,
            ended=self._status not in _ACTIVE,
            end_reason=self._end_reason,
            exit_code=self._exit_code,
            user_typing=self._lease == "user",
            handed_off=self._handoff is not None,
            user_marks=(marks.first, marks.last) if marks is not None else None,
        )

    # ------------------------------------------------------------------ 只读投影

    def snapshot_entry(self) -> dict[str, object]:
        """Transport snapshot 条目（camelCase）；与 ``project_task_sessions`` 契约一致。"""
        with self._lock:
            return {
                "sessionId": self.session_id,
                "label": self.label or "",
                "shellKind": self.shell_kind,
                "createdBy": self.created_by,
                "status": self._status,
                "endReason": self._end_reason,
                "exitCode": self._exit_code,
                "integration": self._observation.integration_status,
                "inputHolder": self._lease,
                "commandRunning": self._observation.active_command is not None,
                "handoff": {"reason": self._handoff.reason} if self._handoff else None,
                "createdAt": self.created_at.isoformat(),
            }

    def describe(self) -> dict[str, object]:
        """Agent 侧会话元数据：只在 start / list / close 返回，不随每次观察重复。"""
        with self._lock:
            buffer = self._observation.buffer
            return {
                "session_id": self.session_id,
                "label": self.label,
                "shell_kind": self.shell_kind,
                "created_by": self.created_by,
                "status": self._status,
                "end_reason": self._end_reason,
                "exit_code": self._exit_code,
                "initial_cwd": self.initial_cwd,
                "cwd": self._observation.cwd,
                "integration": {
                    "status": self._observation.integration_status,
                    "reason": self._observation.integration_reason,
                },
                "geometry": {"rows": buffer.rows, "columns": buffer.columns},
                "input_holder": self._lease,
                "handoff": {"reason": self._handoff.reason} if self._handoff else None,
                "keys": sorted(
                    name[4:] for name in self._capabilities if name.startswith("key_")
                ),
                "signals": sorted(
                    name.removeprefix("signal_").removesuffix("_canonical")
                    for name in self._capabilities
                    if name.startswith("signal_")
                ),
                "lines": f"{buffer.first_available_line}-{buffer.last_line}",
            }

    def subscribe(
        self, after_seq: int | None, loop: Any
    ) -> TerminalStreamAttachment:
        with self._lock:
            buffer = self._observation.buffer
            return self._stream.attach(
                after_seq,
                loop,
                status=self._status,
                state={"rows": buffer.rows, "columns": buffer.columns},
            )

    def unsubscribe(self, subscription: Any) -> None:
        with self._lock:
            self._stream.detach(subscription)

    def resync_event(self, after_seq: int | None) -> dict[str, object]:
        with self._lock:
            return self._stream.resync_event(after_seq)
