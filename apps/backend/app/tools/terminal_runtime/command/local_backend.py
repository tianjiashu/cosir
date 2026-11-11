"""宿主机 shell 执行后端（LOCAL）：经 Terminal Worker 的执行模式跑一条命令并交回结局。

本模块只负责「这条命令用什么程序、什么参数启动」与「worker 报回的事件怎样归一成结果」；输出交给
调用方持有的 ``CommandOutput``，不在这里决定怎样给模型与卡片看。不做危险命令判定、不做权限校验、
不组装 ``ToolObservation``（由 ``ExecuteTerminalTool`` 负责）。命令进程由 worker 启动与清理：stdin
置空、stdout / stderr 合并、以新进程组（Windows 为 Job）启动；命令结束时它留下的进程一并被结束
（自行 ``setsid`` 脱离进程组的守护进程除外，留给沙箱），超时与取消先 HUP、TERM，宽限 1 秒再
KILL。后端被强杀时 worker 读到 stdin EOF 自行清理，命令不会成为孤儿。

``shell="auto"`` 与 Windows 的 ``shell="cmd"`` 把命令原文交给 ``<COMSPEC> /c``（POSIX 为
``/bin/sh -c``）解释；其余显式 shell（``powershell`` / ``pwsh`` / POSIX shell）把命令作为单个参数
传给对应 shell。取舍与证据见 ``_resolve_command``。
"""

import base64
import os
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from app.config.logging.logger import log
from app.tools.shell.host_shells import find_shell
from app.tools.terminal_runtime.command.command_output import CommandOutput
from app.tools.terminal_runtime.command.execution_result import ExecutionResult
from app.tools.terminal_runtime.errors import TerminalWorkerUnavailableError
from app.tools.terminal_runtime.worker_client import ProcessTerminalWorker
from app.utils.host_platform import is_windows

# 要求停止后等 worker 报回退出的时限：它清理整组最多 1 秒宽限、排空输出最多 2 秒，留足余量；
# 到点仍无回音由 ``close`` 结束 worker 进程。
_SHUTDOWN_WAIT_SECONDS = 10.0

_WORKER_LOST = "the terminal worker stopped unexpectedly"

StopCause = Literal["timed_out", "cancelled"]


@dataclass(frozen=True)
class _Launch:
    """worker 执行模式的启动参数；``raw_args`` 只在 Windows 上为真（参数按原文进命令行）。"""

    program: str
    args: tuple[str, ...]
    raw_args: bool = False


class _Outcome:
    """worker 报回的结局：读线程写入 ``exit``（或因断开留空），``ended`` set 之后执行线程读取。"""

    def __init__(self) -> None:
        self.ended = threading.Event()
        self.exit: Mapping[str, object] | None = None


class LocalExecutionBackend:
    """宿主机 shell 执行后端：一个实例只跑一条命令。

    ``stop`` 可在任意线程调用（作业撤销的唤醒回调）：命令还没启动时记下、不再启动；已在运行时让
    worker 结束整组进程。结局由 worker 的 ``exit.reason`` 决定（停止请求与命令退出谁先到由 worker
    裁决）；本对象只记得「自己为什么要求停止」（超时或取消，先到者为准），在结局是 ``stopped`` 时
    据此给出 ``timed_out`` / ``cancelled``。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_cause: StopCause | None = None
        self._worker: ProcessTerminalWorker | None = None

    def stop(self) -> None:
        """要求停止这条命令；不等待，结果仍由 ``execute`` 返回。"""
        self._request_stop("cancelled")

    def _request_stop(self, cause: StopCause) -> None:
        """记下停止原因（只有第一次生效），worker 已启动时立即要求它停止。"""
        with self._lock:
            if self._stop_cause is not None:
                return
            self._stop_cause = cause
            worker = self._worker
        if worker is not None:
            worker.shutdown()

    def execute(
        self,
        command: str,
        cwd: str,
        timeout: float | None,
        output: CommandOutput,
        shell: str = "auto",
    ) -> ExecutionResult:
        """在宿主机同步执行一条命令并把输出交给 ``output``。

        参数:
            command: 待执行命令。
            cwd: 工作目录（绝对路径，调用方已校验存在）。
            timeout: 命令级超时秒数；``None`` 不限时（只有停止请求或命令自己结束）。
            output: 调用方创建并持有的输出收集点；本方法只在 worker 读线程里喂入，不封存它（谁创建谁
                ``finish``，封存与「输出 → 实时通道 → 运行中命令」的收尾顺序都在调用方一处）。
            shell: shell 名称；``auto`` 使用宿主默认 shell，显式值直接启动对应 shell，避免嵌套
                shell 的二次解析。

        返回:
            ``ExecutionResult``，结局见 ``CommandEnd``。启动前已被要求停止时不启动命令，直接返回
            ``cancelled``。

        异常:
            不向上抛出。
        """
        try:
            launch = _resolve_command(command, shell)
        except ValueError as exc:
            return _start_failed(str(exc))
        with self._lock:
            if self._stop_cause is not None:
                return ExecutionResult(end="cancelled")
        outcome = _Outcome()
        try:
            worker = ProcessTerminalWorker(f"exec-{uuid.uuid4().hex}")
            worker.start_exec(
                launch.program,
                launch.args,
                raw_args=launch.raw_args,
                cwd=cwd,
                on_event=lambda event: _observe(event, output, outcome),
            )
        except TerminalWorkerUnavailableError as exc:
            return _start_failed(str(exc))
        with self._lock:
            self._worker = worker
            stopping = self._stop_cause is not None
        if stopping:
            # 握手期间到的停止：命令可能已经启动，交给 worker 停下并如实报回谁先结束。
            worker.shutdown()

        try:
            if not outcome.ended.wait(timeout):
                self._request_stop("timed_out")
                outcome.ended.wait(_SHUTDOWN_WAIT_SECONDS)
        finally:
            worker.close()
        with self._lock:
            cause = self._stop_cause
        return _result(outcome.exit, cause, command)


def _result(
    exit: Mapping[str, object] | None,
    cause: StopCause | None,
    command: str,
) -> ExecutionResult:
    """把 worker 的 ``exit`` 与本端的停止原因归一成结果；``exit`` 为空表示 worker 没报结局。"""
    if exit is None:
        if cause is not None:
            # 已要求停止但 worker 到点没报回（已被 ``close`` 结束）：停止原因仍是事实，清理未确认。
            log.warning(
                "terminal_exec_stop_unconfirmed",
                extra={
                    "msg": "要求停止后 Terminal Worker 未报回结局",
                    "data": {"cause": cause, "command_preview": command[:200]},
                },
            )
            return ExecutionResult(end=cause)
        log.warning(
            "terminal_exec_worker_lost",
            extra={
                "msg": "Terminal Worker 在命令结束前断开，结果未知",
                "data": {"command_preview": command[:200]},
            },
        )
        return ExecutionResult(end="worker_failed", failure=_WORKER_LOST)
    reason = exit["reason"]
    if reason == "exited":
        signal = exit.get("signal")
        if isinstance(signal, int):
            return ExecutionResult(end="signaled", signal=signal)
        return ExecutionResult(end="exited", exit_code=cast(int, exit["exit_code"]))
    if reason == "start_failed":
        return _start_failed(str(exit["message"]))
    if reason == "stopped" and cause is not None:
        return ExecutionResult(end=cause)
    # 心跳超时、worker 自身故障，或没有请求却被停止：命令可能已执行，结局未知。
    message = exit.get("message")
    log.warning(
        "terminal_exec_worker_failed",
        extra={
            "msg": "Terminal Worker 未能正常结束命令",
            "data": {"reason": reason, "message": message, "command_preview": command[:200]},
        },
    )
    detail = f"{_WORKER_LOST} ({reason}{f': {message}' if message else ''})"
    return ExecutionResult(end="worker_failed", failure=detail)


def _observe(
    event: Mapping[str, object], output: CommandOutput, outcome: _Outcome
) -> None:
    """worker 读线程里的事件回调：输出进收集点，结局记进 ``outcome``。``exit`` 是最后一个事件。"""
    kind = event.get("type")
    if kind == "output":
        output.feed(base64.b64decode(str(event["data_base64"])))
    elif kind == "exit":
        outcome.exit = event
        outcome.ended.set()
    elif kind == "disconnected":
        outcome.ended.set()
    elif kind == "error":
        # 非致命诊断（清理失败等），结局照常由 ``exit`` 报回；这里只留排查线索。
        log.warning(
            "terminal_exec_worker_error",
            extra={"msg": "Terminal Worker 执行模式报错", "data": {"code": event.get("code")}},
        )


def _start_failed(reason: str) -> ExecutionResult:
    return ExecutionResult(end="start_failed", failure=reason)


def shell_display_name(shell: str) -> str:
    """这条命令实际由哪种 shell 解释，供运行中命令的表头显示。

    与 :func:`_resolve_command` 的 ``auto`` 含义同源：POSIX 是 ``/bin/sh``，Windows 是 ``COMSPEC``。
    """
    if shell != "auto":
        return shell
    return "cmd" if is_windows() else "sh"


def _resolve_command(command: str, shell: str) -> _Launch:
    """把 shell 选择转换为 worker 执行模式的启动参数。

    - ``auto``（以及 Windows 的 ``cmd``）：命令原文交给 shell 解释。POSIX 为 ``/bin/sh -c <命令>``；
      Windows 为 ``<COMSPEC> /c "<命令原文>"``，按原文拼进命令行（``raw_args``），与 Python
      ``subprocess`` 的 ``shell=True`` 同一形态，命令内部的引号原样到达 cmd。
    - 其余显式 shell：``<shell> <启动参数> <命令>``，命令作为**单个参数**经 MSVCRT 规则转义传递。

    转义职责（本函数的契约核心）：参数形态下命令内的引号按 MSVCRT 规则转义成 ``\\"``。
    PowerShell 的参数解析接受 ``\\"``，所以该形态在 ``powershell`` / ``pwsh`` 上可用；cmd 不还原
    ``\\"``，含引号的命令（如 ``dir /a /s /b "C:\\Program Files"``）会变成无意义路径并报「文件名、
    目录名或卷标语法不正确」，故 Windows 的 ``cmd`` 与 ``auto`` 同为命令原文形态。

    不能把显式 shell「统一改成原文形态」的原因：``powershell`` / ``pwsh`` 的命令原文一旦不经参数
    元素、直接拼进命令行，PowerShell 自身的参数解析会吃掉引号并把引号内的空格当作参数分隔符
    （实测 ``-Command Write-Output "a b"`` 输出两行），参数形态才是正确契约。POSIX 没有命令行这
    一层，参数整体传递。

    只解析本机可执行文件与拼装启动参数，不启动进程；未知、平台不支持或未安装的 shell 通过
    ``ValueError`` 交给 execute 转为启动失败结果。
    """

    windows = is_windows()
    if shell == "auto" or (windows and shell == "cmd"):
        if windows:
            comspec = os.environ.get("COMSPEC") or "cmd.exe"
            return _Launch(comspec, (f'/c "{command}"',), raw_args=True)
        return _Launch("/bin/sh", ("-c", command))
    # 可下发的 shell 由平台参数模型约束（POSIX 契约没有 powershell），这里只拼装启动形态。
    if shell == "pwsh" or (windows and shell == "powershell"):
        launch: tuple[str, ...] = ("-NoLogo", "-NoProfile", "-NonInteractive", "-Command")
    elif not windows and shell in {"sh", "bash", "zsh", "fish"}:
        launch = ("-c",)
    else:
        raise ValueError(f"unsupported shell '{shell}'")
    executable = find_shell(shell)
    if executable is None:
        raise ValueError(f"{shell} is not available")
    return _Launch(executable, (*launch, command))
