"""终端命令输出字节的解码：逐行判定编码，按 UTF-8、GBK、宿主机编码顺序尝试。

命令输出是字节流，按块到达；多字节字符可能被块边界切开。解码器只把字节变成文本，不做收集、
整理与截断（那些归 ``command_output.CommandOutput``）。

判定范围是一行而不是整条流：一条命令的输出常混着两种编码（Windows 上 cmd 报 GBK、node / git 写
UTF-8），流里也可能先冒出一段二进制或 latin-1 字节；整条流只判一次，一个坏字节就会把之后的输出全部
解成乱码。0x0A 不会出现在 UTF-8 或 GBK 的多字节序列内部，所以按原始字节里的换行切行是安全的。UTF-8
永远先试：真正的 GBK 文本几乎不会碰巧是合法 UTF-8，GBK 却几乎接受任何字节对。
"""

from __future__ import annotations

import codecs
import locale

# 判定结果「还只有半个字符」：等下一块补齐再判定。
_WAIT = "wait"


class IncrementalOutputDecoder:
    """逐行判定编码的增量解码器；同一行内保留块边界的编码状态，遇到换行重新判定。"""

    def __init__(self) -> None:
        self._preferred = locale.getpreferredencoding(False) or "utf-8"
        self._encodings = tuple(dict.fromkeys(("utf-8", "gbk", self._preferred)))
        # 本行尚未判定编码的字节（从本行第一个非 ASCII 字节起）。
        self._pending = bytearray()
        # 本行已判定的增量解码器；行尾重置为 ``None``。
        self._decoder: codecs.IncrementalDecoder | None = None

    def decode(self, chunk: bytes, *, final: bool = False) -> str:
        """解码一个字节块，保留块尾不完整的多字节字符。

        参数:
            chunk: 从子进程输出流读取的字节块。
            final: 是否为流末尾，决定不完整字节序列是否必须回退解码。

        返回:
            按行判定后的文本；某行所有候选编码都解不开时以 UTF-8 替换字符解码该行。

        异常:
            无；编码失败会回退，不支持的字节以替换字符呈现。

        副作用:
            更新当前行的编码判定与增量解码器状态。
        """
        parts: list[str] = []
        start = 0
        while (newline := chunk.find(b"\n", start)) >= 0:
            parts.append(self._decode_line_part(chunk[start:newline + 1], line_ends=True))
            start = newline + 1
        if start < len(chunk) or final:
            parts.append(self._decode_line_part(chunk[start:], line_ends=final))
        return "".join(parts)

    def _decode_line_part(self, data: bytes, *, line_ends: bool) -> str:
        """解码同一行里的一段字节；``line_ends`` 时这一行到此结束，残缺字节必须回退解码。"""
        if self._decoder is not None:
            text = self._decoder.decode(data, final=line_ends)
            if line_ends:
                self._decoder = None
            return text

        self._pending.extend(data)
        ascii_length = 0
        for value in self._pending:
            if value >= 0x80:
                break
            ascii_length += 1
        prefix = bytes(self._pending[:ascii_length]).decode("ascii")
        del self._pending[:ascii_length]
        if not self._pending:
            return prefix

        undecided = bytes(self._pending)
        encoding = self._probe(undecided, final=line_ends)
        if encoding == _WAIT:
            return prefix
        self._pending.clear()
        if line_ends:
            return prefix + undecided.decode(encoding or "utf-8", errors="replace")
        # 所有候选都解不开时退化为 UTF-8 替换字符，不再等一个永远补不齐的坏字节序列。
        self._decoder = codecs.getincrementaldecoder(encoding or "utf-8")(errors="replace")
        return prefix + self._decoder.decode(undecided, final=False)

    def _probe(self, undecided: bytes, *, final: bool) -> str | None:
        """按优先级严格试解这一行的待判定字节，返回能解开的编码、``_WAIT`` 或 ``None``（都解不开）。

        某个编码解开了、但块尾还挂着半个字符且还没解出任何非 ASCII 字符时返回 ``_WAIT``：只凭半个字符
        分不出编码，最多多等几个字节；已经解出一个完整的非 ASCII 字符就够判定，残缺尾字节交给增量
        解码器，不再扣住整段输出。
        """
        for encoding in self._encodings:
            probe = codecs.getincrementaldecoder(encoding)(errors="strict")
            try:
                text = probe.decode(undecided, final=final)
            except UnicodeDecodeError:
                continue
            pending_bytes, _ = probe.getstate()
            if pending_bytes and not final and not text:
                return _WAIT
            return encoding
        return None
