"""宿主机 shell 执行后端（LOCAL）。

本模块只负责在宿主机跑一条命令并回收完整输出，不做危险命令判定、不做权限
校验、不组装 ``ToolObservation``（这些由 ``ExecuteTerminalTool`` 负责）。
``shell="auto"`` 与 Windows 的 ``shell="cmd"`` 都走 ``shell=True``：命令原文交给
``<COMSPEC> /c``（POSIX 为 ``/bin/sh -c``）解释，命令内部的引号原样到达 shell。其余
显式 shell（``powershell`` / ``pwsh`` / POSIX shell）用 argv 直接启动（``shell=False``），
避免命令先经过 cmd.exe 解析。``cmd`` 不能走 argv 形态：命令内部的引号会被
``list2cmdline`` 转义成 cmd 无法还原的 ``\"``，含引号命令必然失败——取舍与证据见
``_resolve_command`` 的 docstring。Windows 树杀用系统自带 ``taskkill /F /T``，不引第三方依赖
（不重复造轮子）。命令在后端进程的线程里跑（``execute_terminal`` 是 thread 工具，可转到后台），
超时与取消都由这里杀进程树，没有外层工具子进程兜底。
"""

import contextlib
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable

from app.tools.tool_handler.terminal.execution_result import ExecutionResult
from app.tools.tool_handler.terminal.output_collector import _OutputCollector

# POSIX-only 进程组信号在 Windows typeshed 中不存在；用 getattr 在模块级取，
# 既避免 mypy 在 Windows 上报未定义属性，又保证运行时按平台安全支取（None 即跳过）。
_KILLPG = getattr(os, "killpg", None)
_GETPGID = getattr(os, "getpgid", None)
_SIGKILL = getattr(signal, "SIGKILL", None)

# 等待命令时复查取消的时间片：命令退出会立即返回，时间片只决定取消的响应延迟。
_CANCEL_CHECK_SECONDS = 0.25


class LocalExecutionBackend:
    """宿主机 shell 执行后端。"""

    def execute(
        self,
        command: str,
        cwd: str,
        timeout: float,
        output_sink: Callable[[str], None] | None = None,
        shell: str = "auto",
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ExecutionResult:
        """在宿主机同步执行一条命令并回收完整输出。

        参数:
            command: 待执行命令。
            cwd: 工作目录（绝对路径）。
            timeout: 命令级超时秒数。
            output_sink: 可选实时输出回调；传入时读取线程按字节块回传解码后的原始片段，
                包括 ANSI 控制序列与敏感文本，不应用模型输出预算或展示字符上限。
                回调不得抛异常（``TerminalOutputStream.emit`` 自行吸收投递故障）。
            shell: shell 名称；``auto`` 使用现有宿主默认 shell，显式值直接启动对应
                shell，避免嵌套 shell 的二次解析。
            is_cancelled: 可选取消判定；命令运行期间按时间片复查，命中即强杀进程树。命令在后端
                进程的线程里跑（可能已转到后台），取消只能由这里落实，不能依赖调用方杀进程。

        返回:
            ``ExecutionResult``；超时或取消返回 ``exit_code=-1`` 与对应标记，并保留已收集的
            部分输出。

        异常:
            不向上抛出；启动失败转换为 ``exit_code=-1`` 的错误结果。

        副作用:
            派生子进程执行命令；超时或取消经 taskkill(POSIX killpg) 强杀进程树；
            传入 ``output_sink`` 时在读取线程中按可用输出块回调它。
        """
        try:
            command_argv, use_shell = _resolve_command(command, shell)
            process = subprocess.Popen(  # noqa: S603 - shell command is policy-checked upstream
                command_argv,
                shell=use_shell,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=(os.name == "posix"),
            )
        except (OSError, ValueError) as exc:
            return ExecutionResult(
                output=f"failed to start command: {exc}",
                exit_code=-1,
                timed_out=False,
            )

        collector = _OutputCollector(
            process.stdout,
            sink=output_sink,
        )
        reader = threading.Thread(target=collector.run, daemon=True)
        reader.start()

        timed_out = cancelled = False
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            try:
                process.wait(timeout=max(min(remaining, _CANCEL_CHECK_SECONDS), 0))
                break
            except subprocess.TimeoutExpired:
                if is_cancelled is not None and is_cancelled():
                    cancelled = True
                elif remaining <= 0:
                    timed_out = True
                else:
                    continue
            self._kill_tree(process)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
            break

        reader.join(timeout=5)
        if reader.is_alive():
            # 命令进程虽已结束，遗留子进程仍可能持有 stdout。封存当前快照，
            # 避免读取线程在工具结果返回之后继续写入或旁路。
            collector.seal()
        raw = collector.get()
        exit_code = -1 if timed_out or cancelled else (process.returncode or 0)
        return ExecutionResult(
            output=raw,
            exit_code=exit_code,
            timed_out=timed_out,
            cancelled=cancelled,
        )

    def _kill_tree(self, process: subprocess.Popen) -> None:
        """超时或取消时强杀整棵进程树。

        参数:
            process: 已超时或被取消的 ``subprocess.Popen``。

        返回:
            无。

        异常:
            无；清理失败静默处理，调用方仍返回超时结果。

        副作用:
            POSIX：``os.killpg(getpgid(pid), SIGKILL)``（shell 在独立新组）；
            Windows：``taskkill /F /T /PID`` 杀树；失败静默忽略。
        """
        pid = process.pid
        if _KILLPG is not None and _GETPGID is not None and _SIGKILL is not None:
            try:
                _KILLPG(_GETPGID(pid), _SIGKILL)
            except (ProcessLookupError, OSError):
                with contextlib.suppress(OSError):
                    process.kill()
        else:
            with contextlib.suppress(OSError):
                subprocess.run(  # noqa: S603
                    ["taskkill", "/F", "/T", "/PID", str(pid)],  # noqa: S607
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )


def _resolve_command(command: str, shell: str) -> tuple[str | list[str], bool]:
    """把 shell 选择转换为 Popen 命令和 shell 标志。

    返回值语义（两个值必须成对使用，调用方不得只取其一）：
    - ``use_shell=True``：第一个值是需要由 shell 解释的**命令原文字符串**，Python 会以
      ``<COMSPEC> /c "<命令原文>"``（POSIX 为 ``/bin/sh -c``）启动，命令内部的引号原样
      到达 shell。
    - ``use_shell=False``：第一个值是 argv 列表，``command`` 作为**单个参数**直接传给
      显式 shell，不经 cmd.exe 二次解析。

    转义职责（本函数的契约核心）：argv 形态下命令内的引号由 Python 的 ``list2cmdline``
    按 MSVCRT 规则转义成 ``\"``。PowerShell 的参数解析接受 ``\"``，所以该形态在
    ``powershell`` / ``pwsh`` 上可用；cmd 不还原 ``\"``，因此含引号的命令（如
    ``dir /a /s /b "C:\\Program Files"``）会变成无意义路径并报「文件名、目录名或卷标语法
    不正确」，故 Windows 的 ``cmd`` 与 ``auto`` 同为命令原文字符串形态。

    不能把显式 shell「统一改成字符串形态」的原因：``powershell`` / ``pwsh`` 的命令原文
    一旦不经 argv 元素、直接拼进命令行，PowerShell 自身的参数解析会吃掉引号并把引号内的
    空格当作参数分隔符（实测 ``-Command Write-Output "a b"`` 输出两行），argv 形态才是正确
    契约。POSIX 分支无此矛盾：argv 元素整体传递，不经 cmd 解析。

    该函数只解析本机可执行文件与拼装命令行，不启动进程；未知、平台不支持或未安装的 shell
    通过 ``ValueError`` 交给 execute 转为启动失败结果。
    """

    if shell == "auto":
        return command, True

    if os.name == "nt":
        # cmd 与 auto 等价：命令原文交给 <COMSPEC> /c 解释，避免 list2cmdline 把命令内部的
        # 引号转义成 cmd 无法还原的 \"（见 docstring「转义职责」）。cmd 不可用时由 Popen
        # 自身报错，并被 execute 统一转为启动失败结果。
        if shell == "cmd":
            return command, True
        if shell in {"powershell", "pwsh"}:
            executable_name = "powershell.exe" if shell == "powershell" else "pwsh.exe"
            executable = shutil.which(executable_name)
            if not executable:
                raise ValueError(f"{executable_name} is not available")
            return [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ], False
        raise ValueError(f"shell '{shell}' is not available on Windows")

    if shell in {"sh", "bash", "zsh", "fish"}:
        executable = "/bin/sh" if shell == "sh" else shutil.which(shell)
        if not executable:
            raise ValueError(f"{shell} is not available")
        return [executable, "-c", command], False
    if shell in {"powershell", "pwsh"}:
        executable = shutil.which("pwsh")
        if not executable:
            raise ValueError("pwsh is not available")
        return [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ], False
    raise ValueError(f"unsupported shell '{shell}'")
