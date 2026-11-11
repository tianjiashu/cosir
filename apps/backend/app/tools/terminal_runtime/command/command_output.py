"""一条终端命令输出的唯一收集点：解码一次，分给卡片、模型与运行中读取；内存有上限，全文只在磁盘。

为什么是唯一收集点：同一份字节流有三个读者——卡片要原始文本（终端模拟器解释控制序列），模型要
整理后的纯文本，运行中读取要完成行与当前未完成行。分开收集就要解码多次、整理多份，还会让「模型
看到的」与「卡片看到的」来自不同副本；这里一次解码，按读者各取一份有上限的视图。

为什么内存里不保留全文：Agent 会把开发服务器挂在后台运行，一挂可能一小时，完整输出不封顶会撑大
内存、重连首帧与数据库行。给模型的头、尾各不超过模型预算，卡片只留最近
``DISPLAY_OUTPUT_WINDOW_CHARS``，全文在超过模型预算时边跑边写进工作区日志文件。

模型预算由创建者传入，不读 ``MAX_OUTPUT_CHARS``：那是整条模型正文的上限，命令输出只是正文的一部分，
结局标记、失败原因与提示由工具层拼在外面，帧有多长只有它知道（见 ``ExecuteTerminalTool``）。

为什么只在超过模型预算时才写文件：短命令不产生文件，与现有超长输出落盘的时机一致；日志文件是
「模型正文也装不下」时的兜底，不是每条命令的产物。

生命周期：创建者负责 :meth:`finish`（``ExecuteTerminalTool`` 在作业收尾处统一封存，执行后端只喂入）。
本类不打日志：它在 worker 读线程与作业线程里运行，那里没有日志上下文，写盘失败记进
``CommandOutputSummary.log_error``，由调用方在带 Task / 调用标识的完成日志里一并报告。
"""

from __future__ import annotations

import contextlib
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO
from uuid import uuid4

from app.core.tool.terminal_output_window import trim_output_window
from app.tools.terminal_runtime.command.output_decoder import IncrementalOutputDecoder
from app.tools.terminal_runtime.command.output_normalizer import OutputNormalizer
from app.utils.path.workspace_cosir import workspace_tool_artifact_dir


@dataclass(frozen=True)
class CommandOutputSummary:
    """命令结束后交给工具层的输出：卡片窗口、模型用的头尾与日志位置。"""

    display: str
    """原始文本的最近一段（经 :func:`trim_output_window`）。"""
    display_omitted_lines: int
    """卡片窗口前面省略的行数。"""
    model_head: str
    """整理后的文本：没溢出时是全文，溢出时是前「模型预算」个字符。"""
    model_tail: str
    """整理后全文的后缀：不超过模型预算的最后若干整行（没溢出时也有值，此时 head 已是全文）。"""
    model_total_chars: int
    """整理后全文的字符数（含换行）。"""
    model_total_lines: int
    """整理后全文的行数。"""
    log_path: str
    """日志文件的工作区相对路径（POSIX）；没写或写失败时为 ``""``。"""
    log_error: str
    """日志文件创建或写入失败的原因；没失败时为 ``""``。"""
    overflowed: bool
    """整理后全文是否超过模型预算。"""


class CommandOutput:
    """一条命令输出的唯一收集点：解码一次，分给卡片、模型与运行中读取；内存有上限，全文只在磁盘。"""

    def __init__(
        self,
        *,
        workspace_root: Path,
        model_budget_chars: int,
        on_text: Callable[[str], None] | None = None,
        on_lines: Callable[[list[str], str], None] | None = None,
    ) -> None:
        """``model_budget_chars`` 是命令输出在模型正文里能占的字符数：超过就落盘，头尾各按它封顶。
        ``on_text`` 收原始文本片段（卡片实时通道，须自行吸收投递故障）；``on_lines`` 收整理后的
        完成行与当前未完成行（运行中读取）。两者都可能在 worker 读线程里、持本对象的锁被调用。"""
        self._workspace_root = Path(workspace_root).resolve()
        self._budget = model_budget_chars
        self._on_text = on_text
        self._on_lines = on_lines
        self._lock = threading.Lock()
        self._decoder = IncrementalOutputDecoder()
        self._normalizer = OutputNormalizer()
        # 卡片：原始文本的最近一段；省略行数累计。
        self._display = ""
        self._display_omitted_lines = 0
        # 模型：前缀 head（溢出后冻结）与按行保存的尾部 tail（从第一行起始终维护）。
        self._head = ""
        self._tail: deque[str] = deque()
        self._tail_chars = 0
        self._total_chars = 0
        self._total_lines = 0
        self._overflowed = False
        self._log_file: IO[str] | None = None
        self._log_path = ""
        self._log_error = ""
        self._closed = False

    def feed(self, raw_chunk: bytes) -> None:
        """收下一块原始输出（worker 读线程里调用）；已结束后丢弃，不抛异常。"""
        with self._lock:
            if self._closed:
                return
            text = self._decoder.decode(raw_chunk)
            if text:
                self._accept_text(text)

    def finish(self) -> None:
        """冲出解码器尾部与未完成行、关闭日志并封存；重复调用无副作用。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            tail_text = self._decoder.decode(b"", final=True)
            if tail_text:
                self._accept_text(tail_text)
            flushed = self._normalizer.flush()
            self._accept_lines(flushed)
            if self._on_lines is not None:
                self._on_lines(flushed, "")
            self._close_log()

    def summary(self) -> CommandOutputSummary:
        """命令结束后的输出汇总；未 :meth:`finish` 就调用属于代码缺陷。"""
        with self._lock:
            if not self._closed:
                raise RuntimeError("CommandOutput.summary() called before finish()")
            return CommandOutputSummary(
                display=self._display,
                display_omitted_lines=self._display_omitted_lines,
                model_head=self._head,
                model_tail="".join(self._tail),
                model_total_chars=self._total_chars,
                model_total_lines=self._total_lines,
                log_path=self._log_path,
                log_error=self._log_error,
                overflowed=self._overflowed,
            )

    # ------------------------------------------------------------------ 内部：调用方持锁

    def _accept_text(self, text: str) -> None:
        """一块解码文本：先喂卡片，再喂整理器与运行中读取；调用方持锁。"""
        if self._on_text is not None:
            self._on_text(text)
        self._display += text
        self._display, dropped = trim_output_window(self._display)
        self._display_omitted_lines += dropped
        lines = self._normalizer.feed(text)
        self._accept_lines(lines)
        if self._on_lines is not None:
            self._on_lines(lines, self._normalizer.pending_line())

    def _accept_lines(self, lines: list[str]) -> None:
        """整理后的完成行逐行计入模型通道；调用方持锁。"""
        for line in lines:
            entry = line + "\n"
            self._total_chars += len(entry)
            self._total_lines += 1
            self._append_tail(entry)
            if self._overflowed:
                self._write_log(entry)
                continue
            self._head += entry
            if len(self._head) > self._budget:
                self._begin_overflow()

    def _begin_overflow(self) -> None:
        """跨过模型预算：全文落盘、head 截成前缀并冻结；tail 不受影响（它本来就在维护）。"""
        self._overflowed = True
        self._open_log()
        self._write_log(self._head)
        self._head = self._head[: self._budget]

    def _append_tail(self, entry: str) -> None:
        """tail 始终是全文最后不超过模型预算的整行。

        从第一行起就维护，而不是溢出后才开始攒：``fit_head_tail`` 要求尾部至少有它要取的长度（约
        预算的一半）或就是全文。单行上限由整理器保证（约 ``4 × OBSERVATION_LINE_MAX_CHARS``），
        弹出后 tail 至少还有模型预算减一行的长度，前提恒成立。
        """
        self._tail.append(entry)
        self._tail_chars += len(entry)
        while len(self._tail) > 1 and self._tail_chars > self._budget:
            popped = self._tail.popleft()
            self._tail_chars -= len(popped)

    def _open_log(self) -> None:
        """按现有 artifact 目录与命名创建日志文件；失败只记原因，之后不再尝试写。"""
        target = workspace_tool_artifact_dir(self._workspace_root) / f"{uuid4().hex}.txt"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # 日志文件要跨多次 feed 保持打开：这里不能用 with。
            self._log_file = open(  # noqa: SIM115 - 跨 feed 写同一个流
                target, "w", encoding="utf-8", newline=""
            )
            self._log_path = target.relative_to(self._workspace_root).as_posix()
        except (OSError, ValueError) as exc:
            self._log_file = None
            self._log_path = ""
            self._log_error = f"{target}: {exc}"

    def _write_log(self, text: str) -> None:
        """边跑边写整理后的全文；写入失败后放弃落盘，内存仍有上限，主流程不受影响。"""
        log_file = self._log_file
        if log_file is None:
            return
        try:
            log_file.write(text)
        except OSError as exc:
            self._log_error = f"{self._log_path}: {exc}"
            self._close_log()
            self._log_path = ""

    def _close_log(self) -> None:
        log_file = self._log_file
        self._log_file = None
        if log_file is None:
            return
        with contextlib.suppress(OSError):
            log_file.close()
