"""终端命令输出的解码与完整收集。

本模块只负责把执行后端交来的输出字节块解码成文本，保留完整的已收到文本，并可选旁路发送原样
运行期文本片段。命令的启动、超时与进程清理由执行后端（经 Terminal Worker）负责。
"""

import codecs
import locale
from collections.abc import Callable
from threading import RLock


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
    """按到达顺序收下合并流的字节块，保留完整已收文本并旁路回传原始增量。

    终端 UI 需要把控制字符交给终端模拟器解释，因此这里不做 head/tail 截断、预算裁剪或
    插入截断标记；模型上下文的长度控制由 ``ToolOutputBudget`` 在 handler 返回后负责。
    ``sink`` 的契约是不抛异常（实时通道自行吸收投递故障），这里不包裹。
    """

    def __init__(self, sink: Callable[[str], None] | None = None) -> None:
        self._chunks: list[str] = []
        self._sink = sink
        self._sealed = False
        self._state_lock = RLock()
        self._decoder = _IncrementalOutputDecoder()

    def feed(self, raw_chunk: bytes) -> None:
        """收下一块输出（worker 读线程里调用）；封存后丢弃。"""
        with self._state_lock:
            if not self._sealed:
                self._accept(self._decoder.decode(raw_chunk))

    def finish(self) -> str:
        """冲出解码器尾部、封存并返回完整输出。

        封存之后迟到的块（连接关闭时还在路上的帧）不再收集也不再旁路，返回给调用方的结果与
        实时视图停在同一处，且不追加任何伪造的截断标记。
        """
        with self._state_lock:
            if not self._sealed:
                self._accept(self._decoder.decode(b"", final=True))
                self._sealed = True
            return "".join(self._chunks)

    def _accept(self, text: str) -> None:
        """把解码文本加入完整缓存并旁路发送；调用方持锁。"""
        if not text:
            return
        self._chunks.append(text)
        if self._sink is not None:
            self._sink(text)


__all__ = ["_OutputCollector"]
