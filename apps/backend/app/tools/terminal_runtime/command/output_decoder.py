"""终端命令输出字节的解码：在字节块边界保留编码状态，按 UTF-8、GBK、宿主机编码顺序尝试。

命令输出是字节流，按块到达；多字节字符可能被块边界切开。解码器只把字节变成文本，不做收集、
整理与截断（那些归 ``command_output.CommandOutput``）。
"""

from __future__ import annotations

import codecs
import locale


class IncrementalOutputDecoder:
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
