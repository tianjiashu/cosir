"""终端子进程输出的解码与有界收集。

本模块只负责消费终端执行后端提供的输出流，维护有界最终输出，并可选旁路发送原样运行期
文本片段。子进程的启动、超时与进程树清理由执行后端负责。
"""

import codecs
import contextlib
import locale
from collections import deque
from threading import RLock
from typing import Any

from app.core.tools.schemas.tool_output import OutputSink

MAX_OUTPUT_CHARS = 200_000
HEAD_CHARS = 100_000
TAIL_CHARS = 100_000
TRUNCATION_MARKER = "...[output truncated: {n} chars omitted]..."


class _IncrementalOutputDecoder:
    """在字节块边界保留编码状态，按 UTF-8、GBK、宿主机编码顺序解码。"""

    def __init__(self) -> None:
        self._preferred = locale.getpreferredencoding(False) or "utf-8"
        self._encodings = tuple(dict.fromkeys(("utf-8", "gbk", self._preferred)))
        self._pending = bytearray()
        self._decoder: codecs.IncrementalDecoder | None = None

    def decode(self, chunk: bytes, *, final: bool = False) -> str:
        """解码一个字节块，保留块尾不完整的多字节字符。

        参数:
            chunk: 从子进程输出流读取的字节块。
            final: 是否为流末尾，决定不完整字节序列是否必须回退解码。

        返回:
            UTF-8、GBK 或宿主机首选编码解码的文本；都失败时以替换字符解码。

        异常:
            无；编码失败会回退，不支持的字节以替换字符呈现。

            副作用:
                更新当前输出流的增量解码器状态。
        """
        if self._decoder is not None:
            return self._decoder.decode(chunk, final=final)

        self._pending.extend(chunk)
        ascii_length = 0
        for value in self._pending:
            if value >= 0x80:
                break
            ascii_length += 1
        prefix = bytes(self._pending[:ascii_length]).decode("ascii")
        if ascii_length:
            del self._pending[:ascii_length]
        if not self._pending:
            return prefix

        undecided = bytes(self._pending)
        for encoding in self._encodings:
            probe = codecs.getincrementaldecoder(encoding)(errors="strict")
            try:
                probe.decode(undecided, final=final)
            except UnicodeDecodeError:
                continue
            pending_bytes, _ = probe.getstate()
            if pending_bytes and not final:
                return prefix
            self._decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
            self._pending.clear()
            return prefix + self._decoder.decode(undecided, final=final)

        if not final:
            # 在编码判定完成前最多保留本次块；异常字节流退化为 UTF-8 replacement，
            # 避免等待一个永远不会补齐的坏字节序列。
            self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            self._pending.clear()
            return prefix + self._decoder.decode(undecided, final=False)
        self._pending.clear()
        return prefix + undecided.decode("utf-8", errors="replace")


class _OutputCollector:
    """单读取线程 drain 合并流，做 head+tail 有界截断，并可选实时回传原始增量。

    超过 ``MAX_OUTPUT_CHARS`` 时保留前 ``HEAD_CHARS`` 与后 ``TAIL_CHARS``，中间
    丢弃并计数，避免管道写满反压卡死子进程，也避免无限内存增长。

    若构造时传入 ``sink``，每读到一批子进程字节解码后直接调用 ``sink(text, truncated)``；
    ANSI 控制序列和敏感文本原样保留。实时通道失败不影响最终输出采集。
    """

    def __init__(
        self,
        stream: Any,
        sink: OutputSink | None = None,
        stream_budget: int = 0,
    ) -> None:
        """构造输出采集器。

        参数:
            stream: 子进程合并输出流（``Popen.stdout``），按固定大小字节块读取。
            sink: 可选实时片段回调，``truncated=True`` 表示流式预算已耗尽。
            stream_budget: 实时通道累计字符预算；``<=0`` 表示不限。

        返回:
            无。

        异常:
            无。

        副作用:
            保存输出流和 sink 引用，初始化有界 head/tail 缓冲；不启动线程或读取流。
        """
        self._stream = stream
        self._head: list[str] = []
        self._head_len = 0
        self._tail: deque[str] = deque()
        self._tail_len = 0
        self._omitted = 0
        self._truncated = False
        self._incomplete = False
        self._sink = sink
        self._stream_budget = stream_budget
        self._streamed_len = 0
        self._stream_closed = False
        self._sealed = False
        self._state_lock = RLock()
        self._decoder = _IncrementalOutputDecoder()

    def _emit(self, text: str) -> None:
        """原样发送一段运行期输出；旁路失败时关闭 sink。

        参数:
            text: 已解码的输出文本块，保留 ANSI 控制序列和敏感文本。

        返回:
            无。

        异常:
            不向上抛出；sink 抛错时关闭实时通道，保留最终输出采集。

        副作用:
            可能调用 sink 并更新实时通道预算状态。
        """
        if not text:
            return
        with self._state_lock:
            sink = self._stream_sink_locked()
            if sink is None:
                return
            truncated = False
            if self._stream_budget > 0:
                remaining = self._stream_budget - self._streamed_len
                if remaining <= 0:
                    self._signal_stream_truncated_locked()
                    return
                if len(text) > remaining:
                    text = text[:remaining]
                    truncated = True
                    self._stream_closed = True
            self._streamed_len += len(text)
            try:
                sink(text, truncated)
            except Exception:
                self._stream_closed = True

    def _stream_sink_locked(self) -> OutputSink | None:
        """Return the live callback while it is open and ``_state_lock`` is held."""
        if self._stream_closed:
            return None
        return self._sink

    def _signal_stream_truncated_locked(self) -> None:
        """在已持有 ``_state_lock`` 时关闭实时通道并通知消费者。"""
        if self._sink is None or self._stream_closed:
            return
        self._stream_closed = True
        with contextlib.suppress(Exception):
            self._sink("", True)

    def seal(self) -> None:
        """Freeze collected output after the bounded reader join expires.

        The reader may still be blocked on a descendant-held pipe. Sealing prevents later
        mutations and sink callbacks, and marks the final result incomplete before the handler
        returns, so the process bridge can safely place its completion marker last.
        """
        with self._state_lock:
            self._sealed = True
            self._incomplete = True
            self._signal_stream_truncated_locked()

    def _store(self, text: str) -> None:
        """把解码文本加入有界 head/tail 最终输出缓存。"""
        if not text:
            return
        with self._state_lock:
            if self._sealed:
                return
            if self._head_len < HEAD_CHARS:
                head_text = text[: HEAD_CHARS - self._head_len]
                self._head.append(head_text)
                self._head_len += len(head_text)
                text = text[len(head_text) :]
            if not text:
                return
            self._tail.append(text)
            self._tail_len += len(text)
            while self._tail_len > TAIL_CHARS and self._tail:
                excess = self._tail_len - TAIL_CHARS
                first = self._tail[0]
                if excess >= len(first):
                    self._tail.popleft()
                    self._tail_len -= len(first)
                    self._omitted += len(first)
                else:
                    self._tail[0] = first[excess:]
                    self._tail_len -= excess
                    self._omitted += excess

    def run(self) -> None:
        """以固定大小字节块读取至流关闭，填充有界缓存并旁路回传运行期片段。

        参数:
            无。

        返回:
            无。

        异常:
            不处理底层流读取异常；sink 异常由 ``_emit`` 吸收，不影响最终收集。

        副作用:
            持续消费 ``stream``，更新增量解码器、head/tail 缓冲和截断计数，并可能调用 sink。
        """
        read_chunk = getattr(self._stream, "read1", None) or self._stream.read
        while raw_chunk := read_chunk(4096):
            text = self._decoder.decode(raw_chunk)
            self._store(text)
            self._emit(text)
        final_text = self._decoder.decode(b"", final=True)
        self._store(final_text)
        self._emit(final_text)
        with self._state_lock:
            if self._omitted > 0:
                self._truncated = True

    def get(self) -> str:
        """返回最终有界输出，必要时在保留的首尾片段间标记省略量。

        参数:
            无。

        返回:
            未超限时返回采集文本；超限时返回保留的首尾文本及截断标记。

        异常:
            无。

        副作用:
            无；不修改缓冲内容。
        """
        with self._state_lock:
            if not self._truncated and not self._incomplete:
                return "".join(self._head + list(self._tail))
            head_str = "".join(self._head)
            tail_str = "".join(self._tail)
            if self._incomplete:
                marker = "...[output truncated: additional output unavailable]..."
                if self._omitted > 0:
                    marker = (
                        f"...[output truncated: {self._omitted} chars omitted; "
                        "additional output unavailable]..."
                    )
            else:
                marker = TRUNCATION_MARKER.format(n=self._omitted)
            return f"{head_str}\n{marker}\n{tail_str}"

    @property
    def truncated(self) -> bool:
        """返回最终输出是否因容量限制或封存而不完整。"""
        with self._state_lock:
            return self._truncated or self._incomplete


__all__ = ["MAX_OUTPUT_CHARS", "_OutputCollector"]
