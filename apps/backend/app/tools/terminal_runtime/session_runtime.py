"""单会话执行协调：会话锁保护观察、条件等待、Agent 填入与唯一终态迁移。

service 拥有会话集合、Task 归属与容量；本对象不访问 registry。会话锁内只做内存操作：外部副作用
（snapshot 重投影）经 service 注入的 ``schedule`` 交给 service 的单线程副作用执行器；终态迁移的赢家
由 service 在锁外调用 ``dispose_worker`` 清理 worker。因此 worker 读线程回调本对象时只会短暂持锁，
不做阻塞 IO。

并发约定：
- 等待都在 ``self._condition`` 上进行：worker 事件 ``notify_all`` 唤醒，
  ``WAIT_CANCEL_CHECK_SECONDS`` 时间片只用于复查 Run 取消信号；
- Agent 只能把一行命令填进空提示符（不执行、不等输出），准入只读 shell 集成给出的事实：集成 ready、
  停在提示符、且这个提示符出现后还没有任何输入。用户输入从不被拒，也从不经过准入；
- worker 握手（最多数秒）不在会话锁内进行：启动期间快照投影、订阅与 WS 输入都要拿会话锁，
  其中 snapshot 刷新持有全局投影锁、WS 在事件循环里调用，长持会话锁会拖住所有 Task 与事件循环。
  握手与 worker 关闭由独立的启动互斥 ``_start_lock`` 串行化。``starting`` 期间用户按键与尺寸照常
  进入 worker 的有序输入队列，握手后按序发出（与真实终端的预输入一致）；Agent 填入的准入先判会话
  状态，只在 ``running`` 后接受。
"""

from __future__ import annotations

import base64
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from app.config.constant import Constant
from app.config.logging.logger import log
from app.tools.terminal_runtime.errors import (
    TerminalIntegrationRequiredError,
    TerminalPrefillRejectedError,
    TerminalSessionStateError,
    TerminalWorkerUnavailableError,
)
from app.tools.terminal_runtime.inputs import TerminalWait
from app.tools.terminal_runtime.line_buffer import TerminalLineBuffer
from app.tools.terminal_runtime.observation import InputSource, TerminalObservation
from app.tools.terminal_runtime.render import (
    KeepMode,
    LineSearch,
    SessionFacts,
    TerminalObservationResult,
    render_observation,
    render_session_summary,
)
from app.tools.terminal_runtime.shell_resolver import ShellSpec
from app.tools.terminal_runtime.stream import (
    TerminalStreamAttachment,
    TerminalStreamBuffer,
)
from app.tools.terminal_runtime.wait import WaitBaseline, WaitEvaluator
from app.tools.terminal_runtime.worker_client import (
    ProcessTerminalWorker,
    WorkerEventCallback,
)

RunCancellationCheck = Callable[[], bool]
EffectScheduler = Callable[[Callable[[], None]], None]
UserInputResult = Literal["accepted", "session_ended"]

_ACTIVE = frozenset({"starting", "running"})
# worker ``exit.reason`` → 会话失败原因；``exited`` 是 shell 自己结束，单独处理。会话的终态只来自
# ``exit`` 与读线程补发的 ``disconnected``，``error`` 帧从不代表终态（见 Worker 协议）。
_WORKER_EXIT_FAILURES = {
    "stopped": "worker_stopped",
    "heartbeat_timeout": "worker_heartbeat_timeout",
    "failed": "worker_failed",
}


def refresh_task_sessions(task_id: int) -> None:
    """影响会话条目的变化（生命周期、集成）后整表重投影该 Task 的终端会话列表。

    终端运行时只关心会话本身；触达应用层的动作在函数内导入，避免终端包导入时拉起应用层。
    由 service 的单线程副作用执行器在会话锁外调用，失败由执行器记录，不影响会话。
    """
    from app.service import depends

    depends.get_conversation_task_state_service().refresh_terminal_sessions(task_id)


@dataclass(frozen=True)
class SessionSnapshot:
    """会话在某一时刻的只读事实，取自同一次会话锁内。

    是 runtime 唯一的对外投影：工具卡片、模型摘要与 Transport 会话条目都从它派生；camelCase 的
    wire 形状由 ``assistant_transport`` 映射，不在这里产出。
    """

    session_id: str
    shell_kind: str
    initial_cwd: str
    created_at: datetime
    status: str
    end_reason: str | None
    exit_code: int | None
    integration: str
    command_running: bool
    # 给模型的一行摘要（terminal_list 用）：身份、当前状态与最近命令。
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
        initial_cwd: str,
        spec: ShellSpec,
        schedule: EffectScheduler,
    ) -> None:
        """只建内存状态与 worker 句柄（解析 worker 路径，未配置时抛
        ``TerminalWorkerUnavailableError``），不启动子进程；握手见 :meth:`start`。"""
        # 身份和用途在会话创建后不变；生命周期字段只在会话锁内更新。
        self.session_id: Final = f"term_{uuid.uuid4().hex[:12]}"
        self.task_id: Final = task_id
        self.initial_cwd: Final = initial_cwd
        self.spec: Final = spec
        self.created_at: Final = datetime.now(UTC)
        self._schedule = schedule
        self._status = "starting"
        self._end_reason: str | None = None
        self._exit_code: int | None = None
        # 句柄随会话常驻；写入前按会话状态拒绝（见 ``_admit_prefill`` 与 ``user_input``），
        # 终态后只有 ``dispose_worker`` 再碰它。
        self._worker: Final = ProcessTerminalWorker(uuid.uuid4().hex)
        # 会话锁：可重入（持锁的辅助方法互相调用），等待都在它上面进行。
        self._condition = threading.Condition(threading.RLock())
        # 只串行化「worker 握手」与「worker 关闭」，从不在会话锁内获取，避免与读线程互等。
        self._start_lock = threading.Lock()
        self._disposal_finished = threading.Event()
        self._stream = TerminalStreamBuffer(
            self.session_id, Constant.Terminal.MAX_RING_BUFFER_BYTES
        )
        self._observation = TerminalObservation(spec.kind, spec.integration)
        self._user_last_input = float("-inf")
        # shell 报告命令开始时据此记下发起方（见 ``TerminalCommand``）：Agent 只填不执行，命令只可能
        # 由用户回车发起；会话启动脚本等之前没有任何输入时为 None。
        self._last_input_by: InputSource | None = None
        self._user_marks = _UserInputMarks()
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
            log.info(
                "terminal_worker_started",
                extra={
                    "msg": "终端 worker 已建立，会话与进程诊断信息已关联",
                    "data": {
                        "task_id": self.task_id,
                        "session_id": self.session_id,
                        "worker_instance_id": worker.instance_id,
                        "worker_pid": worker.pid,
                        "shell_executable": self.spec.executable,
                        "geometry": worker.geometry,
                    },
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

    def finish(
        self,
        status: Literal["closed", "exited", "failed"],
        reason: str,
        exit_code: int | None = None,
    ) -> bool:
        """原子确定终态并唤醒所有等待。

        只有首次迁移返回 True，调用方随后在锁外 ``dispose_worker``；重复迁移返回 False。终态只落
        ``terminal_session_ended`` 日志、不通知 Agent：会话都是用户的，Agent 经 snapshot 与
        ``terminal_list`` / ``terminal_read`` 看到终态。
        """
        with self._condition:
            if self._status not in _ACTIVE:
                return False
            self._observation.end_tracking("session_ended")
            self._end_reason = reason
            self._exit_code = exit_code
            self._status = status
            self._stream.finish()
            self._condition.notify_all()
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
        return True

    def wait_for_disposal(self) -> None:
        """仅供批量清理等待已有句柄持有者；普通回调或重复 close 不等待自身收尾。"""
        self._disposal_finished.wait()

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
            reason = event["reason"]
            if reason == "exited":
                code = event.get("exit_code")
                return self.finish(
                    "exited", "shell_exited", code if isinstance(code, int) else None
                )
            log.warning(
                "terminal_session_worker_ended",
                extra={
                    "msg": "Terminal Worker 结束了会话",
                    "data": {
                        "session_id": self.session_id,
                        "reason": reason,
                        "message": event.get("message"),
                    },
                },
            )
            return self.finish("failed", _WORKER_EXIT_FAILURES[reason])
        elif kind == "disconnected":
            return self.finish("failed", "worker_disconnected")
        elif kind == "error":
            code = event.get("code")
            if code == "input_backpressure":
                # 写入已被接受、事后才在 worker 处因前台程序不读输入而排不进 PTY：
                # 用户与 Agent 的输入都可能是这一次，只能在面板上提示「部分输入未送达」，
                # Agent 从下一次观察里看到效果。
                log.warning(
                    "terminal_session_input_backpressure",
                    extra={
                        "msg": "前台程序未读取输入，worker 写入队列已满，本次输入被丢弃",
                        "data": {"session_id": self.session_id},
                    },
                )
                with self._condition:
                    self._stream.publish("input_rejected", reason="backpressure")
            else:
                log.warning(
                    "terminal_session_worker_error",
                    extra={
                        "msg": "Terminal Worker 报告非致命错误",
                        "data": {
                            "session_id": self.session_id,
                            "code": code,
                            "message": event.get("message"),
                        },
                    },
                )
        return False

    def _shell_event(self, event: dict[str, Any]) -> None:
        with self._condition:
            if self._status not in _ACTIVE:
                return
            kind = self._observation.shell_event(event, last_input_by=self._last_input_by)
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
        return False

    # ------------------------------------------------------------------ Agent 填入

    def prefill(
        self, command: str, *, run_id: int | None, tool_call_id: str
    ) -> TerminalObservationResult:
        """把一行命令填进空提示符，立即返回尾部观察；不执行、也不等输出。

        回车只能由用户按：填入后命令仍在提示符上，用户核对后自己决定是否运行。命令里的回车、控制
        字符与空串在参数模型和 worker 两侧都被拒绝，所以文本不会触发执行，也不会提前结束括号粘贴
        包裹。这里不发等待：用户何时回车、是否运行，由之后的:meth:`read` 观察。

        异常:
            TerminalSessionStateError: 会话尚在握手或已结束。
            TerminalPrefillRejectedError: 该会话当前不满足填入条件（见 :meth:`_admit_prefill`）。
        """
        preview = command[:200]
        with self._condition:
            self._admit_prefill()
            self._observation.note_input()
            self._stream.publish("input_mark", by="agent")
            worker = self._worker
        worker.write({"type": "prefill", "text": command})
        with self._condition:
            self._last_read = None
            log.info(
                "terminal_agent_prefill_accepted",
                extra={
                    "msg": "Agent 把一行命令填进终端提示符（不执行）",
                    "data": {
                        "task_id": self.task_id,
                        "run_id": run_id,
                        "session_id": self.session_id,
                        "tool_call_id": tool_call_id,
                        "command_preview": preview,
                    },
                },
            )
            last = self._observation.buffer.last_line
            return self._build_result(
                last - Constant.Terminal.READ_DEFAULT_TAIL + 1, last, "tail", prefilled=preview
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
        """
        wait_result: dict[str, object] | None = None
        if wait is not None:
            with self._condition:
                integration = self._observation.integration_status
                if wait.until == "command_returned" and integration != "ready":
                    raise TerminalIntegrationRequiredError(
                        "wait until=command_returned needs shell integration, which is "
                        f"{integration} for this session"
                    )
                baseline = WaitBaseline.of(self._observation)
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

        时间片只为复查取消；会话结束时以 ``session_ended`` 作为结束原因返回，不再另发通知。
        """
        with self._condition:
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
            return evaluator.result(reason)

    def notify_changed(self) -> None:
        """影响 snapshot 会话条目的变化：经副作用线程整表重投影该 Task 的会话列表。"""
        task_id = self.task_id
        self._schedule(lambda: refresh_task_sessions(task_id))

    # ------------------------------------------------------------------ 输入仲裁

    def _user_typing(self) -> bool:
        return time.monotonic() - self._user_last_input < Constant.Terminal.USER_TYPING_IDLE_SECONDS

    def _admit_prefill(self) -> None:
        """持锁调用：判定这次填入能否落到提示符，不能时抛错；用户输入从不经过这里。

        会话须在运行（``starting`` 握手未完成、终态后 worker 已不接受输入），其余三个条件都是 shell
        集成与观察给出的既有事实，任一不满足即拒绝：
        - 没有 shell 集成就看不到提示符，无法确认行里是否为空；
        - 有命令在执行（或尚未回到提示符）时，填入会落进正在运行的程序；
        - 这个提示符出现后已经有输入（用户敲了半行，或同一提示符上已经填过一次）时再填会拼进已有
          内容。最后一条也覆盖了旧的幂等缓存：同一个提示符至多填一次。
        """
        if self._status != "running":
            raise TerminalSessionStateError(f"terminal session is {self._status}")
        if self._observation.integration_status != "ready":
            raise TerminalPrefillRejectedError(
                "this shell has no shell integration, so its prompt cannot be detected"
            )
        # 有命令在执行时 phase 必为 executing，这一条同时覆盖「命令在跑」与「尚未回到提示符」。
        if self._observation.phase != "editing":
            raise TerminalPrefillRejectedError(
                "a command is running in this terminal (or it is not at its prompt)"
            )
        if self._observation.prompt_has_input:
            raise TerminalPrefillRejectedError(
                "the prompt already has input (typed by the user or filled earlier)"
            )

    def user_input(self, data: bytes) -> UserInputResult:
        """用户原始按键：可在事件循环里直接调用，只做内存更新与非阻塞入队；从不因 Agent 被拒。

        先入队、成功后才记录按键时间、来源与输入标注：背压（``TerminalWorkerBackpressureError``，
        抛给调用方提示）或 worker 已不在时按键没有送达，状态必须保持原样。入队非阻塞、不与读线程
        交互，可在锁内进行。``starting`` 时照常入队，握手后按序发出。worker 已退出、读线程尚未补发
        ``disconnected`` 的瞬间按会话已结束回报，不让按键无声消失。
        """
        with self._condition:
            if self._status not in _ACTIVE:
                return "session_ended"
            try:
                self._worker.write(
                    {"type": "raw", "data_base64": base64.b64encode(data).decode("ascii")}
                )
            except TerminalWorkerUnavailableError:
                return "session_ended"
            self._user_marks.record(self._observation.buffer)
            self._observation.note_input()
            self._user_last_input = time.monotonic()
            self._last_input_by = "user"
        return "accepted"

    def resize(self, rows: int, columns: int) -> None:
        """与用户按键同一条有序队列；``starting`` 时照常入队，握手后按序发出。"""
        with self._condition:
            if self._status not in _ACTIVE:
                raise TerminalSessionStateError(f"terminal session is {self._status}")
            worker = self._worker
        worker.resize(rows, columns)

    # ------------------------------------------------------------------ 结果渲染

    def _build_result(
        self,
        first: int,
        last: int,
        mode: KeepMode,
        *,
        wait_result: dict[str, object] | None = None,
        prefilled: str | None = None,
        logical: bool = False,
        search: LineSearch | None = None,
    ) -> TerminalObservationResult:
        """持锁调用：渲染观察文本（屏幕视图 / 逻辑行的选择见 ``render_observation``；区间下限由
        它收拢）；用户输入标注在交付后清空。"""
        result = render_observation(
            self._facts(),
            self._observation,
            first,
            last,
            mode,
            logical=logical,
            search=search,
            wait_result=wait_result,
            prefilled=prefilled,
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
            user_typing=self._user_typing(),
            user_marks=self._user_marks.lines,
            user_typed_in_screen=self._user_marks.in_screen,
        )

    # ------------------------------------------------------------------ 只读投影

    def snapshot(self) -> SessionSnapshot:
        with self._condition:
            return SessionSnapshot(
                session_id=self.session_id,
                shell_kind=self.spec.kind,
                initial_cwd=self.initial_cwd,
                created_at=self.created_at,
                status=self._status,
                end_reason=self._end_reason,
                exit_code=self._exit_code,
                integration=self._observation.integration_status,
                command_running=self._observation.active_command is not None,
                summary=render_session_summary(self._facts(), self._observation),
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
