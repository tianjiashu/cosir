"""终端子进程输出的解码与完整收集。

本模块只负责消费终端执行后端提供的输出流，保留完整的已读取文本，并可选旁路发送原样
运行期文本片段。子进程的启动、超时与进程树清理由执行后端负责。
"""

import codecs
import locale
from threading import RLock
from typing import Any

from app.core.tools.schemas.tool_output import OutputSink


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
    """单读取线程 drain 合并流，保留完整已读取输出并旁路回传原始增量。

    终端 UI 需要把控制字符交给终端模拟器解释，因此这里不能做 head/tail 截断、预算裁剪
    或插入截断标记。``stream_budget`` 也不再存在；模型上下文的长度控制由统一的
    ``ToolOutputBudget`` 在 handler 返回后单独负责。若读取线程因进程树清理而被封存，
    只冻结当前已读取快照，不伪造额外文本或截断标记。
    """

    def __init__(
        self,
        stream: Any,
        sink: OutputSink | None = None,
    ) -> None:
        """构造输出采集器。

        参数:
            stream: 子进程合并输出流（``Popen.stdout``），按固定大小字节块读取。
            sink: 可选实时片段回调。回调失败只关闭实时旁路，不影响最终输出采集。

        返回:
            无。

        异常:
            无。

        副作用:
            保存输出流和 sink 引用，初始化完整输出缓冲；不启动线程或读取流。
        """
        self._stream = stream
        self._chunks: list[str] = []
        self._sink = sink
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
            可能调用 sink；sink 失败后关闭实时旁路。
        """
        if not text:
            return
        with self._state_lock:
            sink = self._stream_sink_locked()
            if sink is None:
                return
            try:
                sink(text)
            except Exception:
                self._stream_closed = True

    def _stream_sink_locked(self) -> OutputSink | None:
        """Return the live callback while it is open and ``_state_lock`` is held."""
        if self._stream_closed:
            return None
        return self._sink

    def seal(self) -> None:
        """Freeze collected output after the bounded reader join expires.

        The reader may still be blocked on a descendant-held pipe. Sealing prevents later
        mutations and sink callbacks before the handler returns, so the process bridge can safely
        place its completion marker last. It does not add a synthetic marker to the output.
        """
        with self._state_lock:
            self._sealed = True
            self._stream_closed = True

    def _store(self, text: str) -> None:
        """把解码文本加入完整最终输出缓存。"""
        if not text:
            return
        with self._state_lock:
            if self._sealed:
                return
            self._chunks.append(text)

    def run(self) -> None:
        """以固定大小字节块读取至流关闭，填充完整缓存并旁路回传运行期片段。

        参数:
            无。

        返回:
            无。

        异常:
            不处理底层流读取异常；sink 异常由 ``_emit`` 吸收，不影响最终收集。

        副作用:
            持续消费 ``stream``，更新增量解码器和完整输出缓冲，并可能调用 sink。
        """
        read_chunk = getattr(self._stream, "read1", None) or self._stream.read
        while raw_chunk := read_chunk(4096):
            text = self._decoder.decode(raw_chunk)
            self._store(text)
            self._emit(text)
        final_text = self._decoder.decode(b"", final=True)
        self._store(final_text)
        self._emit(final_text)

    def get(self) -> str:
        """返回已读取的完整最终输出。

        参数:
            无。

        返回:
            所有已成功读取并解码的文本，原样拼接返回。

        异常:
            无。

        副作用:
            无；不修改缓冲内容。
        """
        with self._state_lock:
            return "".join(self._chunks)


__all__ = ["_OutputCollector"]
