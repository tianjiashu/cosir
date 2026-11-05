"""宿主机 shell 执行后端（LOCAL）：经 Terminal Worker 的执行模式跑一条命令并回收完整输出。

本模块只负责「这条命令用什么程序、什么参数启动」与「worker 报回的事件怎样归一成结果」，不做
危险命令判定、不做权限校验、不组装 ``ToolObservation``（由 ``ExecuteTerminalTool`` 负责）。命令
进程由 worker 启动与清理：stdin 置空、stdout / stderr 合并、以新进程组（Windows 为 Job）启动；
命令结束时它留下的进程一并被结束，超时与取消先 HUP、TERM，宽限 1 秒再 KILL。后端被强杀时
worker 读到 stdin EOF 自行清理，命令不会成为孤儿。

``shell="auto"`` 与 Windows 的 ``shell="cmd"`` 把命令原文交给 ``<COMSPEC> /c``（POSIX 为
``/bin/sh -c``）解释；其余显式 shell（``powershell`` / ``pwsh`` / POSIX shell）把命令作为单个参数
传给对应 shell。取舍与证据见 ``_resolve_command``。
"""

import base64
import os
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from app.config.logging.logger import log
from app.tools.shell.host_shells import find_shell
from app.tools.terminal_runtime.errors import TerminalWorkerUnavailableError
from app.tools.terminal_runtime.worker_client import ProcessTerminalWorker
from app.tools.tool_handler.terminal.execution_result import ExecutionResult
from app.tools.tool_handler.terminal.output_collector import _OutputCollector
from app.utils.host_platform import is_windows

# 要求停止后等 worker 报回退出的时限：它清理整组最多 1 秒宽限、排空输出最多 2 秒，留足余量；
# 到点仍无回音由 ``close`` 结束 worker 进程。
_SHUTDOWN_WAIT_SECONDS = 10.0

_WORKER_LOST = "[terminal worker stopped unexpectedly; the command's outcome is unknown]"


@dataclass(frozen=True)
class _Launch:
    """worker 执行模式的启动参数；``raw_args`` 只在 Windows 上为真（参数按原文进命令行）。"""

    program: str
    args: tuple[str, ...]
    raw_args: bool = False


class _Outcome:
    """worker 事件里与结局有关的部分；读线程写入，``ended`` set 之后执行线程读取。"""

    def __init__(self) -> None:
        self.ended = threading.Event()
        self.exit: Mapping[str, object] | None = None
        self.start_failure: str | None = None
        self.worker_lost = False


class LocalExecutionBackend:
    """宿主机 shell 执行后端：一个实例只跑一条命令。

    ``stop`` 可在任意线程调用（作业撤销的唤醒回调）：命令还没启动时记下，启动后立即结束；
    已在运行时让 worker 结束整组进程。被停止的命令以 ``cancelled`` 结果返回。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stopped = False
        self._worker: ProcessTerminalWorker | None = None

    def stop(self) -> None:
        """要求停止这条命令；不等待，结果仍由 ``execute`` 返回。"""
        with self._lock:
            self._stopped = True
            worker = self._worker
        if worker is not None:
            worker.shutdown()

    def execute(
        self,
        command: str,
        cwd: str,
        timeout: float,
        output_sink: Callable[[str], None] | None = None,
        shell: str = "auto",
    ) -> ExecutionResult:
        """在宿主机同步执行一条命令并回收完整输出。

        参数:
            command: 待执行命令。
            cwd: 工作目录（绝对路径，调用方已校验存在）。
            timeout: 命令级超时秒数。
            output_sink: 可选实时输出回调；在 worker 读线程里按字节块回传解码后的原始片段，
                包括 ANSI 控制序列与敏感文本，不应用模型输出预算或展示字符上限。回调不得抛异常
                （``TerminalOutputStream.emit`` 自行吸收投递故障）。
            shell: shell 名称；``auto`` 使用宿主默认 shell，显式值直接启动对应 shell，避免嵌套
                shell 的二次解析。

        返回:
            ``ExecutionResult``。超时或停止返回 ``exit_code=-1`` 与对应标记，并保留已收到的部分
            输出；被信号结束的命令退出码为负的信号值；启动失败（shell 不可用、找不到程序、worker
            起不来）为 ``exit_code=-1`` 且输出写明原因。

        异常:
            不向上抛出。
        """
        try:
            launch = _resolve_command(command, shell)
        except ValueError as exc:
            return _start_failed(str(exc))
        collector = _OutputCollector(sink=output_sink)
        outcome = _Outcome()
        try:
            worker = ProcessTerminalWorker(f"exec-{uuid.uuid4().hex}")
            worker.start_exec(
                launch.program,
                launch.args,
                raw_args=launch.raw_args,
                cwd=cwd,
                on_event=lambda event: _observe(event, collector, outcome),
            )
        except TerminalWorkerUnavailableError as exc:
            return _start_failed(str(exc))
        with self._lock:
            self._worker = worker
            stopped = self._stopped
        if stopped:
            worker.shutdown()

        timed_out = False
        try:
            if not outcome.ended.wait(timeout):
                timed_out = True
                worker.shutdown()
                outcome.ended.wait(_SHUTDOWN_WAIT_SECONDS)
        finally:
            worker.close()
        output = collector.finish()
        with self._lock:
            cancelled = self._stopped and not timed_out
        if outcome.start_failure is not None:
            return _start_failed(outcome.start_failure)
        if timed_out or cancelled:
            return ExecutionResult(output, -1, timed_out=timed_out, cancelled=cancelled)
        if outcome.exit is None:
            log.warning(
                "terminal_exec_worker_lost",
                extra={
                    "msg": "Terminal Worker 在命令结束前断开，结果未知",
                    "data": {"worker_lost": outcome.worker_lost, "command_preview": command[:200]},
                },
            )
            lost = f"{output}\n{_WORKER_LOST}" if output else _WORKER_LOST
            return ExecutionResult(lost, -1, timed_out=False)
        return ExecutionResult(output, _exit_code(outcome.exit), timed_out=False)


def _observe(event: Mapping[str, object], collector: _OutputCollector, outcome: _Outcome) -> None:
    """worker 读线程里的事件回调：输出进收集器，结局记进 ``outcome``。``exit`` 是最后一个事件。"""
    kind = event.get("type")
    if kind == "output":
        collector.feed(base64.b64decode(str(event["data_base64"])))
    elif kind == "exit":
        outcome.exit = event
        outcome.ended.set()
    elif kind == "error":
        code = event.get("code")
        if code == "EXEC_START_FAILED":
            outcome.start_failure = str(event.get("message") or "the command could not start")
            outcome.ended.set()
        elif code == "WORKER_DISCONNECTED":
            outcome.worker_lost = True
            outcome.ended.set()
        else:
            # 清理失败、心跳超时等：命令照常会报回退出，这里只留排查线索。
            log.warning(
                "terminal_exec_worker_error",
                extra={"msg": "Terminal Worker 执行模式报错", "data": {"code": code}},
            )


def _exit_code(event: Mapping[str, object]) -> int:
    """与 ``subprocess`` 的 ``returncode`` 同一语义：被信号结束为负的信号值。"""
    signal = event.get("signal")
    if isinstance(signal, int):
        return -signal
    code = event.get("exit_code")
    return code if isinstance(code, int) else -1


def _start_failed(reason: str) -> ExecutionResult:
    return ExecutionResult(
        output=f"failed to start command: {reason}", exit_code=-1, timed_out=False
    )


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
