"""宿主机 shell 执行后端（LOCAL）。

本模块只负责在宿主机跑一条命令并回收有界输出，不做危险命令判定、不做权限
校验、不组装 ``ToolObservation``（这些由 ``ExecuteTerminalTool`` 负责）。
``shell="auto"`` 用系统默认（``shell=True`` 复用 Windows ``cmd.exe`` / POSIX
``/bin/sh``）；显式 shell 使用 argv 直接启动（``shell=False``）。Windows 树杀用系统
自带 ``taskkill /F /T``，Job Object 由 ``tool_handler_runner`` 子进程入口负责，均不引
第三方依赖（不重复造轮子）。
"""

import contextlib
import os
import shutil
import signal
import subprocess
import threading

from app.core.tools.schemas.tool_output import OutputSink
from app.core.tools.tool_handler.terminal.execution_backend import ExecutionBackend
from app.core.tools.tool_handler.terminal.execution_result import ExecutionResult
from app.core.tools.tool_handler.terminal.output_collector import (
    _OutputCollector,
)

# POSIX-only 进程组信号在 Windows typeshed 中不存在；用 getattr 在模块级取，
# 既避免 mypy 在 Windows 上报未定义属性，又保证运行时按平台安全支取（None 即跳过）。
_KILLPG = getattr(os, "killpg", None)
_GETPGID = getattr(os, "getpgid", None)
_SIGKILL = getattr(signal, "SIGKILL", None)


class LocalExecutionBackend(ExecutionBackend):
    """宿主机 shell 执行后端（v1 唯一实现）。"""

    def execute(
        self,
        command: str,
        cwd: str,
        timeout: float,
        output_sink: OutputSink | None = None,
        shell: str = "auto",
    ) -> ExecutionResult:
        """在宿主机同步执行一条命令并回收有界输出。

        参数:
            command: 待执行命令。
            cwd: 工作目录（绝对路径）。
            timeout: 命令级超时秒数。
            output_sink: 可选实时输出回调；传入时读取线程按字节块回传解码后的原始片段，
                包括 ANSI 控制序列与敏感文本，不应用模型输出预算或展示字符上限。
                回调异常不影响命令执行与最终输出。
            shell: shell 名称；``auto`` 使用现有宿主默认 shell，显式值直接启动对应
                shell，避免嵌套 shell 的二次解析。

        返回:
            ``ExecutionResult``；超时返回 ``timed_out=True``、``exit_code=-1`` 且保留
            已收集的部分输出。

        异常:
            不向上抛出；启动失败转换为 ``exit_code=-1`` 的错误结果。

        副作用:
            派生子进程执行命令；超时经 taskkill(POSIX killpg) 强杀进程树；
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
                truncated=False,
                timed_out=False,
            )

        collector = _OutputCollector(
            process.stdout,
            sink=output_sink,
        )
        reader = threading.Thread(target=collector.run, daemon=True)
        reader.start()

        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_tree(process)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)

        reader.join(timeout=5)
        if reader.is_alive():
            # 命令进程虽已结束，遗留子进程仍可能持有 stdout。封存当前一致快照并关闭 sink，
            # 避免读取线程在工具结果/跨进程完成标记之后继续写入。
            collector.seal()
        raw = collector.get()
        exit_code = -1 if timed_out else (process.returncode or 0)
        return ExecutionResult(
            output=raw,
            exit_code=exit_code,
            truncated=collector.truncated,
            timed_out=timed_out,
        )

    def _kill_tree(self, process: subprocess.Popen) -> None:
        """超时强杀整棵进程树（内层命令级超时兜底）。

        参数:
            process: 已超时的 ``subprocess.Popen``。

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

    ``auto`` 保留 Python 当前的宿主默认 shell 行为；显式 shell 使用 argv 直接启动，
    从而避免 PowerShell 命令先经过 cmd.exe 解析。该函数只解析本机可执行文件，不启动
    进程；未知、平台不支持或未安装的 shell 通过 ``ValueError`` 交给 execute 转为启动
    失败结果。
    """

    if shell == "auto":
        return command, True

    if os.name == "nt":
        if shell == "cmd":
            executable = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
            if not executable:
                raise ValueError("cmd.exe is not available")
            return [executable, "/d", "/s", "/c", command], False
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
