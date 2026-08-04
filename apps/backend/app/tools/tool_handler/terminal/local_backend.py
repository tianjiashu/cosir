"""宿主机 shell 执行后端（LOCAL）。

本模块只负责在宿主机跑一条命令并回收有界输出，不做危险命令判定、不做权限
校验、不组装 ``ToolObservation``（这些由 ``ExecuteTerminalTool`` 负责）。
shell 用系统默认（``shell=True`` 复用 Windows ``cmd.exe`` / POSIX ``/bin/sh``），
Windows 树杀用系统自带 ``taskkill /F /T``，Job Object 由 ``tool_executor`` 子进程
入口负责，均不引第三方依赖（不重复造轮子）。
"""

import contextlib
import locale
import os
import re
import signal
import subprocess
import threading
from collections import deque
from typing import Any

from app.tools.tool_handler.terminal.execution_backend import ExecutionBackend
from app.tools.tool_handler.terminal.execution_result import ExecutionResult

# POSIX-only 进程组信号在 Windows typeshed 中不存在；用 getattr 在模块级取，
# 既避免 mypy 在 Windows 上报未定义属性，又保证运行时按平台安全支取（None 即跳过）。
_KILLPG = getattr(os, "killpg", None)
_GETPGID = getattr(os, "getpgid", None)
_SIGKILL = getattr(signal, "SIGKILL", None)

MAX_OUTPUT_CHARS = 200_000
HEAD_CHARS = 100_000
TAIL_CHARS = 100_000
TRUNCATION_MARKER = "...[output truncated: {n} chars omitted]..."

# ANSI 剥离正则（自实现，覆盖 CSI / OSC / 字符集切换三类）：
# - CSI：  \x1b[ ... 中间字节 ... 终结字节（含 ?25l 等私有参数）
# - OSC：  \x1b] ... \x07(BEL) 或 \x1b\\(ST) 终止，非贪婪避免吞相邻文本
# - 字符集切换：\x1b(B / \x1b)0 等
# 自实现原因（规范"三查"）：代码库无现成实现；生态无与 npm strip-ansi 对等的
# 单一职责库（rich 体积大且面向渲染非剥离）；标准库无内置。故自写并锁行为于测试。
_ANSI_PATTERN = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*?(?:\x07|\x1b\\)"  # OSC (BEL 或 ST)
    r"|\x1b[()][0-9A-Z]"  # 字符集切换
)


def _strip_ansi(text: str) -> str:
    """剥离 ANSI 转义序列，保留可读文本。"""
    return _ANSI_PATTERN.sub("", text)


def _decode_line(raw: bytes) -> str:
    """把子进程输出的一行原始字节解码为文本。

    优先按 UTF-8 解码；失败时回退到 GBK（覆盖 Windows 子进程常见的 CP936 输出，
    无论宿主机 locale 为何），再回退到系统首选编码；避免把非 UTF-8 字节错误地
    解释为替换字符，进而造成终端输出中文乱码。所有编码均失败时退化为
    ``errors="replace"``（仅用于展示，不会被写回文件）。
    """
    preferred = locale.getpreferredencoding(False) or "utf-8"
    for enc in ("utf-8", "gbk", preferred):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


class _OutputCollector:
    """单读取线程 drain 合并流，做 head+tail 有界截断。

    超过 ``MAX_OUTPUT_CHARS`` 时保留前 ``HEAD_CHARS`` 与后 ``TAIL_CHARS``，中间
    丢弃并计数，避免管道写满反压卡死子进程，也避免无限内存增长。
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._head: list[str] = []
        self._head_len = 0
        self._tail: deque[str] = deque()
        self._tail_len = 0
        self._omitted = 0
        self._truncated = False
        self._lock = threading.Lock()

    def run(self) -> None:
        phase = "head"
        for raw_line in self._stream:
            line = _decode_line(raw_line)
            if phase == "head":
                if self._head_len + len(line) <= HEAD_CHARS:
                    self._head.append(line)
                    self._head_len += len(line)
                else:
                    space = HEAD_CHARS - self._head_len
                    self._head.append(line[:space])
                    self._head_len = HEAD_CHARS
                    rest = line[space:]
                    if rest:
                        self._tail.append(rest)
                        self._tail_len += len(rest)
                    phase = "tail"
            else:
                self._tail.append(line)
                self._tail_len += len(line)
                while self._tail_len > TAIL_CHARS and self._tail:
                    dropped = self._tail.popleft()
                    self._tail_len -= len(dropped)
                    self._omitted += len(dropped)
        if self._omitted > 0:
            self._truncated = True

    def get(self) -> str:
        with self._lock:
            if not self._truncated:
                return "".join(self._head + list(self._tail))
            head_str = "".join(self._head)
            tail_str = "".join(self._tail)
            return f"{head_str}\n{TRUNCATION_MARKER.format(n=self._omitted)}\n{tail_str}"

    @property
    def truncated(self) -> bool:
        return self._truncated


class LocalExecutionBackend(ExecutionBackend):
    """宿主机 shell 执行后端（v1 唯一实现）。"""

    def execute(self, command: str, cwd: str, timeout: float) -> ExecutionResult:
        """在宿主机同步执行一条命令并回收有界输出。

        参数:
            command: 待执行命令。
            cwd: 工作目录（绝对路径）。
            timeout: 命令级超时秒数。

        返回:
            ``ExecutionResult``；超时返回 ``timed_out=True``、``exit_code=-1`` 且保留
            已收集的部分输出。

        异常:
            不向上抛出；启动失败转换为 ``exit_code=-1`` 的错误结果。

        副作用:
            派生子进程执行命令；超时经 taskkill(POSIX killpg) 强杀进程树。
        """
        try:
            process = subprocess.Popen(  # noqa: S602
                command,
                shell=True,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            return ExecutionResult(
                output=f"failed to start command: {exc}",
                exit_code=-1,
                truncated=False,
                timed_out=False,
            )

        collector = _OutputCollector(process.stdout)
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
        raw = collector.get()
        exit_code = -1 if timed_out else (process.returncode or 0)
        return ExecutionResult(
            output=_strip_ansi(raw),
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
            不向上抛出。

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
