"""终端子进程输出的解码与完整收集。

本模块只负责消费终端执行后端提供的输出流，保留完整的已读取文本，并可选旁路发送原样
运行期文本片段。子进程的启动、超时与进程树清理由执行后端负责。
"""

import codecs
import locale
from collections.abc import Callable
from threading import RLock
from typing import Any


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

    终端 UI 需要把控制字符交给终端模拟器解释，因此这里不做 head/tail 截断、预算裁剪或
    插入截断标记；模型上下文的长度控制由 ``ToolOutputBudget`` 在 handler 返回后负责。
    ``sink`` 的契约是不抛异常（实时通道自行吸收投递故障），这里不包裹。
    """

    def __init__(
        self,
        stream: Any,
        sink: Callable[[str], None] | None = None,
    ) -> None:
        """``stream`` 为子进程合并输出流（``Popen.stdout``）；构造时不读流、不起线程。"""
        self._stream = stream
        self._chunks: list[str] = []
        self._sink = sink
        self._sealed = False
        self._state_lock = RLock()
        self._decoder = _IncrementalOutputDecoder()

    def seal(self) -> None:
        """读取线程 join 超时后冻结已读取快照。

        遗留子进程仍可能持有管道、读取线程还会醒来；封存后不再收集也不再旁路，保证返回给
        调用方的结果与实时视图都停在同一处，且不追加任何伪造的截断标记。
        """
        with self._state_lock:
            self._sealed = True

    def _accept(self, text: str) -> None:
        """把解码文本加入完整缓存并旁路发送；封存后丢弃。"""
        if not text:
            return
        with self._state_lock:
            if self._sealed:
                return
            self._chunks.append(text)
            if self._sink is not None:
                self._sink(text)

    def run(self) -> None:
        """以固定大小字节块读取至流关闭；底层读取异常不处理，由读取线程终止。"""
        read_chunk = getattr(self._stream, "read1", None) or self._stream.read
        while raw_chunk := read_chunk(4096):
            self._accept(self._decoder.decode(raw_chunk))
        self._accept(self._decoder.decode(b"", final=True))

    def get(self) -> str:
        """返回已读取（封存时为封存那一刻）的完整输出。"""
        with self._state_lock:
            return "".join(self._chunks)


__all__ = ["_OutputCollector"]
