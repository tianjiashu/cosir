"""单会话执行协调：会话锁保护输入租约、观察、条件等待、交接及唯一终态迁移。

service 拥有会话集合、Task 归属与容量；本对象不访问 registry。会话锁内只做内存操作：
外部副作用（snapshot 重投影、会话结束通知、交接代为落定）都在 ``session_effects``，经 service
注入的 ``schedule`` 交给 service 的单线程副作用执行器；终态迁移的赢家由
service 在锁外调用 ``dispose_worker`` 清理 worker。因此 worker 读线程回调本对象时只会短暂持锁，
不做阻塞 IO。

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
import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Final, Literal

from app.config.constant import Constant
from app.config.logging.logger import log
from app.tools.terminal_runtime import session_effects
from app.tools.terminal_runtime.errors import (
    TerminalInputHeldByUserError,
    TerminalIntegrationRequiredError,
    TerminalSessionError,
    TerminalSessionStateError,
)
from app.tools.terminal_runtime.line_buffer import TerminalLineBuffer
from app.tools.terminal_runtime.render import (
    KeepMode,
    LineSearch,
    SessionFacts,
    TerminalObservationResult,
    render_end_notice,
    render_observation,
    render_session_summary,
)
from app.tools.terminal_runtime.worker_client import (
    ProcessTerminalWorker,
    WorkerEventCallback,
)
from app.tools.terminal_runtime.shell_resolver import ShellSpec
from app.tools.terminal_runtime.inputs import (
    DEFAULT_WRITE_WAIT,
    TerminalWait,
    require_input_capability,
    summarize_input,
)
from app.tools.terminal_runtime.observation import TerminalObservation
from app.tools.terminal_runtime.stream import (
    TerminalStreamAttachment,
    TerminalStreamBuffer,
)
from app.tools.terminal_runtime.wait import WaitBaseline, WaitEvaluator

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
class _Handoff:
    """进行中的交接：按发起它的 ``terminal_handoff`` 调用寻址，交还时从 ``from_line`` 报告用户做了什么。"""

    tool_call_id: str
    reason: str
    from_line: int


@dataclass(frozen=True)
class SessionSnapshot:
    """会话在某一时刻的只读事实，取自同一次会话锁内。

    是 runtime 唯一的对外投影：工具卡片、模型摘要与 Transport 会话条目都从它派生；camelCase 的
    wire 形状由 ``assistant_transport`` 映射，不在这里产出。
    """

    session_id: str
    label: str | None
    shell_kind: str
    created_by: Literal["agent", "user"]
    initial_cwd: str
    created_at: datetime
    status: str
    end_reason: str | None
    exit_code: int | None
    integration: str
    input_holder: LeaseHolder | None
    # 只在 Agent 仍持有租约时给出（租约可能已被用户的 Ctrl-C 转走）。
    agent_input_call_id: str | None
    command_running: bool
    handoff_reason: str | None
    handoff_call_id: str | None
    # 给模型的一行摘要（start / close / list 共用）：身份、创建者、当前状态与最近命令。
    summary: str


@dataclass
class _UserInputMarks:
    """用户输入标注，Agent 下一次观察交付后清空。

    主屏记光标所在逻辑行的范围；全屏程序里光标行是冻结的主屏行，标它会误导，只记「有输入」。
    """

    lines: tuple[int, int] | None = None
    in_screen: bool = False

    @property
    def empty(self) -> bool:
        return self.lines is None and not self.in_screen

    def record(self, buffer: TerminalLineBuffer) -> None:
        if buffer.alternate:
            self.in_screen = True
            return
        cursor = buffer.cursor_line
        first, last = self.lines or (cursor, cursor)
        self.lines = (min(first, cursor), max(last, cursor))


class TerminalSessionRuntime:
    """单会话的状态与并发边界；不访问会话集合。"""

    def __init__(
        self,
        *,
        task_id: int,
        created_by: Literal["agent", "user"],
        initial_cwd: str,
        spec: ShellSpec,
        label: str | None,
        schedule: EffectScheduler,
    ) -> None:
        """只建内存状态与 worker 句柄（解析 worker 路径，未配置时抛 ``TerminalWorkerUnavailableError``），
        不启动子进程；握手见 :meth:`start`。"""
        # 身份和用途在会话创建后不变；生命周期字段只在会话锁内更新。
        self.session_id: Final = f"term_{uuid.uuid4().hex[:12]}"
        self.task_id: Final = task_id
        self.created_by: Final = created_by
        self.initial_cwd: Final = initial_cwd
        self.spec: Final = spec
        self.label: Final = label
        self.created_at: Final = datetime.now(UTC)
        self._schedule = schedule
        self._status = "starting"
        self._end_reason: str | None = None
        self._exit_code: int | None = None
        # 句柄随会话常驻；终态后 ``_mutable_worker`` 按状态拒绝，只有 ``dispose_worker`` 再碰它。
        self._worker: Final = ProcessTerminalWorker(uuid.uuid4().hex)
        # 会话锁：可重入（持锁的辅助方法互相调用），等待都在它上面进行。
        self._condition = threading.Condition(threading.RLock())
        # 只串行化「worker 握手」与「worker 关闭」，从不在会话锁内获取，避免与读线程互等。
        self._start_lock = threading.Lock()
        self._pending_geometry: tuple[int, int] | None = None
        self._disposal_finished = threading.Event()
        self._stream = TerminalStreamBuffer(self.session_id, Constant.Terminal.MAX_RING_BUFFER_BYTES)
        self._observation = TerminalObservation(spec.kind, spec.integration)
        self._accepted_writes: OrderedDict[str, bytes] = OrderedDict()
        self._lease: LeaseHolder | None = None
        # Agent 持有租约时对应的 terminal_write 调用；用户「接管输入」据此把它的前台等待转到后台。
        self._agent_call_id: str | None = None
        self._user_last_input = 0.0
        self._lease_watch_running = False
        self._user_marks = _UserInputMarks()
        self._handoff: _Handoff | None = None
        # 正在会话条件变量上进行的 Agent 条件等待；会话结束时它们会把结果带给 Agent，不必另发结束通知。
        self._agent_waits = 0
        # 上一次无等待读取的（信息版本, 状态, 请求参数），用于识别「无变化的重复读取」。
        self._last_read: tuple[object, ...] | None = None

    # ------------------------------------------------------------------ 生命周期

    @property
    def active(self) -> bool:
        """会话仍处于 starting / running；集合筛选只读这一项，完整快照必须在会话锁内读取。"""
        return self._status in _ACTIVE

    def start(self, on_event: WorkerEventCallback) -> None:
        """在会话锁外完成 worker 握手，成功后迁移为 running。

        握手期间会话可被并发结束（Task 删除、关闭）：``finish`` 照常在会话锁内迁移终态并交出句柄，
        worker 的关闭经 :meth:`dispose_worker` 等握手结束后再进行，慢启动与删除交错时句柄不会脱管，
        也不会在 ``worker.start`` 半途被关闭。握手失败的异常抛给 service 收尾。
        """
        worker = self._worker
        with self._start_lock:
            with self._condition:
                if self._status != "starting":
                    raise TerminalSessionStateError(f"terminal session is {self._status}")
            worker.start(self.spec, self.initial_cwd, on_event)
            with self._condition:
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
                        "shell_executable": self.spec.executable,
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
        self.notify_changed()

    def dispose_worker(self) -> None:
        """由终态迁移的赢家调用一次：先等进行中的握手结束，避免与 ``worker.start`` 并发关闭。

        无论关闭成败都标记释放尝试已完成（:meth:`wait_for_disposal` 只等这一步，不等观察者）；
        关闭异常抛给调用方。
        """
        try:
            with self._start_lock:
                self._worker.close()
        finally:
            self._disposal_finished.set()

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
            self.notify_changed()

    def finish(
        self,
        status: Literal["closed", "exited", "failed"],
        reason: str,
        exit_code: int | None = None,
        *,
        announce: bool,
    ) -> bool:
        """原子确定终态并唤醒所有等待。

        只有首次迁移返回 True，调用方随后在锁外 ``dispose_worker``；重复迁移返回 False。

        ``announce`` 由调用方声明这次结束是否是 Agent 不知道的（shell 退出、worker 故障、用户
        关闭）；Agent 自己关闭、启动失败（错误直接返回给调用方）、Task 删除与后端关闭不需要告知。
        仅当会话由 Agent 创建、且没有条件等待或交接会把 ``session_ended`` 带回给它时，经副作用
        执行器投递一条结束通知（系统通知，不唤醒 Run，随下一次推进并入）。交接中的会话结束、且这次
        结束需要告知时，由运行时代用户落定那次 HITL 调用（见 ``session_effects.settle_handoff``）；后端关闭与
        Task 删除不代为落定：等待中的决定按 HITL 契约保留，之后交还会报告会话已不在。
        """
        with self._condition:
            if self._status not in _ACTIVE:
                return False
            self._observation.end_tracking("session_ended")
            self._end_reason = reason
            self._exit_code = exit_code
            self._status = status
            self._lease = None
            self._stream.finish()
            self._condition.notify_all()
            handoff, self._handoff = self._handoff, None
            notice = (
                render_end_notice(
                    self._facts(), self._observation, label=self.label, created_by=self.created_by
                )
                if announce
                and self.created_by == "agent"
                and handoff is None
                and self._agent_waits == 0
                else None
            )
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
                    "announced": notice is not None,
                },
            },
        )
        if notice is not None:
            self._schedule(lambda: session_effects.post_session_ended_notice(self.task_id, notice))
        if handoff is not None and announce:
            self._schedule(
                lambda: session_effects.settle_handoff(self.task_id, handoff.tool_call_id)
            )
        return True

    def wait_for_disposal(self) -> None:
        """仅供批量清理等待已有句柄持有者；普通回调或重复 close 不等待自身收尾。"""
        self._disposal_finished.wait()

    def _mutable_worker(self) -> ProcessTerminalWorker:
        """持锁调用：取 running 会话的 worker；``starting`` 时握手未完成、worker 尚不接受输入。"""
        if self._status != "running":
            raise TerminalSessionStateError(f"terminal session is {self._status}")
        return self._worker

    # ------------------------------------------------------------------ worker 事件

    def on_worker_event(self, event: Mapping[str, Any]) -> bool:
        """worker 读线程内调用：只做内存更新与唤醒；首次终态迁移返回 True（调用方负责清理）。"""
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
            return self.finish(
                "exited", "shell_exited", code if isinstance(code, int) else None, announce=True
            )
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
                return self.finish("failed", reason, announce=True)
        return False

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
        self.notify_changed()

    def _output(self, event: Mapping[str, Any]) -> bool:
        # 缺失或非字符串的 ``data_base64`` 与解码失败同属 worker 协议违约，一律收敛会话。
        encoded = event.get("data_base64")
        if not isinstance(encoded, str):
            return self.finish("failed", "invalid_worker_output", announce=True)
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            return self.finish("failed", "invalid_worker_output", announce=True)
        if not data or len(data) > Constant.Terminal.MAX_WORKER_OUTPUT_CHUNK_BYTES:
            return self.finish("failed", "worker_output_chunk_too_large", announce=True)
        with self._condition:
            if self._status in _ACTIVE:
                self._stream.append(encoded, len(data))
                self._observation.output_arrived()
                self._condition.notify_all()
        return False

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
        """投递一次 Agent 输入并按条件等待；输入接受后的取消仍返回接受事实，避免模型重发。

        等待只在前台、在工具线程里进行，输入租约持有到等待结束；长时间的工作交给 ``execute_terminal``
        的后台执行，不在交互终端里挂着等。交接是独立的 HITL 工具，见 :meth:`begin_handoff`。

        worker 不可用（含信号回执超时）只作为本次调用的错误抛出，不结束会话：会话终态只来自 worker
        事件流（``exit`` / 致命 ``error`` / 读线程断开）与显式关闭，在命令路径上判死会误伤回执慢的
        正常会话。
        """
        kind = input["type"]
        summary = summarize_input(input)
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
                return self._build_result(
                    self._default_tail_start(),
                    self._observation.buffer.last_line,
                    "tail",
                    note="duplicate operation_id: input was already accepted and was not resent",
                    input_type=kind,
                    input_summary=summary,
                )
            if is_cancelled():
                raise TerminalSessionStateError("terminal input was not accepted: run cancelled")
            worker = self._mutable_worker()
            require_input_capability(input, worker.capabilities)
            self._acquire_agent_lease(is_cancelled)
            self._agent_call_id = tool_call_id
            baseline = WaitBaseline.of(
                self._observation, after_line_input=kind == "line", after_input=True
            )
            if kind in {"line", "text", "keys"}:
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
            wait_result = self._wait(WaitEvaluator(spec, baseline), is_cancelled)
            with self._condition:
                return self._build_result(
                    baseline.line,
                    self._observation.buffer.last_line,
                    "both",
                    wait_result=wait_result,
                    input_type=kind,
                    input_summary=summary,
                )
        finally:
            with self._condition:
                self._release_agent_lease()

    def _deliver_agent_input(self, worker: ProcessTerminalWorker, input: Mapping[str, Any]) -> None:
        """锁外投递：line / text / keys 只入队；signal 等待 worker 回执，持锁会与读线程互等。"""
        kind = input["type"]
        if kind == "signal":
            worker.signal(str(input["signal"]))
        elif kind in {"line", "text"}:
            worker.write({"type": kind, "text": input["text"]})
        else:
            worker.write({"type": "keys", "keys": list(input["keys"])})

    def _require_wait_supported(self, spec: TerminalWait) -> None:
        if spec.until == "command_returned" and self._observation.integration_status != "ready":
            raise TerminalIntegrationRequiredError(
                "wait until=command_returned needs shell integration, which is "
                f"{self._observation.integration_status} for this session; "
                "use until=output_quiet or until=match instead"
            )

    # ------------------------------------------------------------------ Agent 读取

    def read(
        self,
        *,
        start_line: int | None = None,
        limit: int | None = None,
        tail: int | None = None,
        pattern: str | None = None,
        wait: TerminalWait | None = None,
        is_cancelled: RunCancellationCheck,
    ) -> TerminalObservationResult:
        """按行读取；带 wait 时先按条件等待再读。无变化且无等待的重复读取只给一行提示。

        ``pattern`` 把读取变成搜索：从 ``start_line``（缺省为最早保留行）扫到末行，只返回命中行，
        ``limit`` 限制命中数；搜索总是按逻辑行（全屏程序期间搜冻结的主屏）。正则已在参数边界校验。
        等待规则同 :meth:`write`。
        """
        wait_result: dict[str, object] | None = None
        if wait is not None:
            with self._condition:
                self._require_wait_supported(wait)
                baseline = WaitBaseline.of(
                    self._observation, after_line_input=False, after_input=False
                )
            # 没有执行中的命令就没有可等的返回，直接给出空闲结果。
            if wait.until == "command_returned" and baseline.active is None:
                wait_result = {"until": wait.until, "end_reason": "idle"}
            else:
                wait_result = self._wait(WaitEvaluator(wait, baseline), is_cancelled)
        with self._condition:
            buffer = self._observation.buffer
            mode: KeepMode
            search: LineSearch | None = None
            max_limit = Constant.Terminal.READ_MAX_LIMIT
            if pattern is not None:
                first = start_line or buffer.first_available_line
                last, mode = buffer.last_line, "head"
                search = LineSearch(re.compile(pattern), min(limit or max_limit, max_limit))
            elif start_line is not None:
                count = min(limit or max_limit, max_limit)
                first, last, mode = start_line, start_line + count - 1, "head"
            else:
                count = min(tail or limit or Constant.Terminal.READ_DEFAULT_TAIL, max_limit)
                first, last, mode = buffer.last_line - count + 1, buffer.last_line, "tail"
            logical = start_line is not None or search is not None
            # 全屏程序期间，不指定 start_line 的读取就是读整屏，整屏总在变动区。
            screen_view = buffer.alternate and not logical
            signature = (
                self._observation.information_version,
                self._status,
                start_line,
                limit,
                tail,
                pattern,
            )
            touches_live = screen_view or last >= buffer.live_from_line
            if (
                wait is None
                and touches_live
                and self._last_read == signature
                and self._user_marks.empty
            ):
                cursor = (
                    f"cursor s{buffer.screen_cursor_row}"
                    if screen_view
                    else f"cursor line {buffer.cursor_line}"
                )
                return TerminalObservationResult(
                    session_id=self.session_id,
                    status=self._status,
                    text=(
                        f"session {self.session_id}: no change since your last read "
                        f"({cursor}). Use wait to block until something happens instead of "
                        "reading again."
                    ),
                    screen=screen_view,
                )
            self._last_read = signature if wait is None else None
            return self._build_result(
                first, last, mode, wait_result=wait_result, logical=logical, search=search
            )

    # ------------------------------------------------------------------ 条件等待

    def _wait(
        self, evaluator: WaitEvaluator, is_cancelled: RunCancellationCheck
    ) -> dict[str, object]:
        """不持锁调用：在工具线程里持会话锁等条件（worker 事件 ``notify_all`` 唤醒），返回等待结果。

        ``_agent_waits`` 让会话结束时不再另发结束通知：等待结果本身会告诉 Agent，见 :meth:`finish`。
        时间片只为复查取消。
        """
        with self._condition:
            self._agent_waits += 1
            try:
                while True:
                    if is_cancelled():
                        reason = "cancelled"
                        break
                    if self._status not in _ACTIVE:
                        reason = "session_ended"
                        break
                    now = time.monotonic()
                    checked, due = evaluator.check(self._observation, now)
                    if checked is not None:
                        reason = checked
                        break
                    self._condition.wait(
                        min(max(due - now, 0.0), Constant.Terminal.WAIT_CANCEL_CHECK_SECONDS)
                    )
            finally:
                self._agent_waits -= 1
            return evaluator.result(reason)

    # ------------------------------------------------------------------ 交接

    def begin_handoff(self, reason: str, tool_call_id: str) -> TerminalObservationResult:
        """登记一次交接（``terminal_handoff`` 首次调用），返回给卡片展示的当前尾部。

        交接只是会话上的提示与 snapshot 投影：不持输入租约（用户本来就能输入），也不登记延迟结果——
        等待交给 HITL：工具返回等待占位挂起 Agent，用户交还即对这次调用作出决定。
        """
        with self._condition:
            self._mutable_worker()
            if self._handoff is not None:
                raise TerminalSessionStateError("a handoff is already active for this session")
            buffer = self._observation.buffer
            self._handoff = _Handoff(
                tool_call_id=tool_call_id, reason=reason, from_line=buffer.last_line
            )
            result = self._build_result(
                self._default_tail_start(),
                buffer.last_line,
                "tail",
                input_type="handoff",
                input_summary=reason,
            )
        log.info(
            "terminal_handoff_started",
            extra={
                "msg": "Agent 把终端输入权交给用户",
                "data": {
                    "task_id": self.task_id,
                    "session_id": self.session_id,
                    "tool_call_id": tool_call_id,
                },
            },
        )
        self.notify_changed()
        return result

    def end_handoff(self, tool_call_id: str) -> TerminalObservationResult:
        """交接的决定到达后重放 ``terminal_handoff``：清除交接，报告交接以来的输出。

        会话仍活着时从交接那一刻的末行报告到现在（用户输入的行照常标注）；会话已结束时报告尾部并
        说明。交接已不在（重复决定、已被清除）时同样只给尾部，不报错：决定只会执行一次，结果总要
        让等待占位收尾。
        """
        with self._condition:
            handoff = self._handoff
            if handoff is not None and handoff.tool_call_id == tool_call_id:
                self._handoff = None
            else:
                handoff = None
            buffer = self._observation.buffer
            if self._status not in _ACTIVE:
                result = self._build_result(
                    self._default_tail_start(),
                    buffer.last_line,
                    "tail",
                    note="the session ended before the user handed it back",
                    input_type="handoff",
                )
            else:
                result = self._build_result(
                    handoff.from_line if handoff is not None else self._default_tail_start(),
                    buffer.last_line,
                    "both",
                    note="the user handed the terminal back to you",
                    input_type="handoff",
                    input_summary=handoff.reason if handoff is not None else None,
                )
        log.info(
            "terminal_handoff_completed",
            extra={
                "msg": "终端交接结束",
                "data": {
                    "task_id": self.task_id,
                    "session_id": self.session_id,
                    "tool_call_id": tool_call_id,
                    "active": self._status in _ACTIVE,
                },
            },
        )
        if handoff is not None:
            self.notify_changed()
        return result

    def notify_changed(self) -> None:
        """影响 snapshot 会话条目的变化：经副作用线程整表重投影（见 ``session_effects``）。"""
        task_id = self.task_id
        self._schedule(lambda: session_effects.refresh_task_sessions(task_id))

    # ------------------------------------------------------------------ 输入租约

    def _acquire_agent_lease(self, is_cancelled: RunCancellationCheck) -> None:
        """持锁调用：等用户租约释放最多 ``AGENT_LEASE_WAIT_SECONDS``，超时报可重试错误。"""
        deadline = time.monotonic() + Constant.Terminal.AGENT_LEASE_WAIT_SECONDS
        waited = False
        while self._lease is not None:
            waited = True
            if self._status not in _ACTIVE:
                raise TerminalSessionStateError(f"terminal session is {self._status}")
            if is_cancelled():
                raise TerminalSessionStateError("terminal input was not accepted: run cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.info(
                    "terminal_agent_lease_timeout",
                    extra={
                        "msg": "Agent 等待终端输入租约超时",
                        "data": {"session_id": self.session_id, "holder": self._lease},
                    },
                )
                if self._lease == "user":
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
        self._agent_call_id = None
        if self._lease == "agent":
            self._set_lease(None)

    def _set_lease(self, holder: LeaseHolder | None) -> None:
        """持锁调用：租约变化经快照投影给前端（唯一事实源，WS 不另推）；等待者被唤醒复查。"""
        if self._lease == holder:
            return
        self._lease = holder
        self._condition.notify_all()
        self.notify_changed()

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
            self._user_marks.record(self._observation.buffer)
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
        """用户主动释放输入租约；不结束交接（交接只由交还即 HITL 决定结束，见 :meth:`end_handoff`）。"""
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
        logical: bool = False,
        search: LineSearch | None = None,
    ) -> TerminalObservationResult:
        """持锁调用：渲染观察文本（屏幕视图 / 逻辑行的选择见 ``render_observation``）；
        用户输入标注在交付后清空。"""
        result = render_observation(
            self._facts(),
            self._observation,
            first,
            last,
            mode,
            logical=logical,
            search=search,
            wait_result=wait_result,
            note=note,
            input_type=input_type,
            input_summary=input_summary,
        )
        self._user_marks = _UserInputMarks()
        return result

    def _facts(self) -> SessionFacts:
        """持锁调用：渲染所需的会话侧事实。"""
        return SessionFacts(
            session_id=self.session_id,
            status=self._status,
            ended=self._status not in _ACTIVE,
            end_reason=self._end_reason,
            exit_code=self._exit_code,
            user_typing=self._lease == "user",
            handed_off=self._handoff is not None,
            user_marks=self._user_marks.lines,
            user_typed_in_screen=self._user_marks.in_screen,
        )

    # ------------------------------------------------------------------ 只读投影

    def snapshot(self) -> SessionSnapshot:
        with self._condition:
            handoff = self._handoff
            return SessionSnapshot(
                session_id=self.session_id,
                label=self.label,
                shell_kind=self.spec.kind,
                created_by=self.created_by,
                initial_cwd=self.initial_cwd,
                created_at=self.created_at,
                status=self._status,
                end_reason=self._end_reason,
                exit_code=self._exit_code,
                integration=self._observation.integration_status,
                input_holder=self._lease,
                agent_input_call_id=self._agent_call_id if self._lease == "agent" else None,
                command_running=self._observation.active_command is not None,
                handoff_reason=handoff.reason if handoff else None,
                handoff_call_id=handoff.tool_call_id if handoff else None,
                summary=render_session_summary(
                    self._facts(), self._observation, label=self.label, created_by=self.created_by
                ),
            )

    def subscribe(self, after_seq: int | None, loop: Any) -> TerminalStreamAttachment:
        with self._condition:
            buffer = self._observation.buffer
            return self._stream.attach(
                after_seq, loop, status=self._status, rows=buffer.rows, columns=buffer.columns
            )

    def unsubscribe(self, subscription: Any) -> None:
        with self._condition:
            self._stream.detach(subscription)

    def resync_event(self, after_seq: int | None) -> dict[str, object]:
        with self._condition:
            return self._stream.resync_event(after_seq)
