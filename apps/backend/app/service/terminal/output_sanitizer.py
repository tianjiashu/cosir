"""交互终端 Agent 输出的流式控制序列过滤器。"""

from __future__ import annotations

import codecs


class TerminalOutputSanitizer:
    """跨 PTY 输出块移除 ANSI/OSC 控制序列，保留普通文本和常用文本控制符。

    该投影只供 Agent 读取，不重建光标位置或终端屏幕；原始字节仍由预览通道交给 xterm。
    过滤器按 session 持有，因此转义序列或 UTF-8 字符被 PTY 分到多个输出块时不会泄漏残片。
    """

    _STRING_INTRODUCERS = frozenset({"P", "X", "^", "_"})
    _OSC_INTRODUCERS = {"]", "\x9d"}
    _STRING_C1_INTRODUCERS = {"\x90", "\x98", "\x9e", "\x9f"}

    def __init__(self) -> None:
        """初始化 UTF-8 解码器与跨块转义序列状态。"""

        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._state = "text"
        self._string_escape = False
        self._finished = False

    def feed(self, data: bytes) -> str:
        """过滤一个 PTY 输出块，并保留尚未结束的编码或控制序列状态。"""

        if self._finished:
            raise RuntimeError("terminal output sanitizer is finished")
        return self._filter(self._decoder.decode(data, final=False))

    def finish(self) -> str:
        """刷新会话末尾不完整的 UTF-8 字符；未闭合控制序列仍按控制内容丢弃。"""

        if self._finished:
            return ""
        self._finished = True
        return self._filter(self._decoder.decode(b"", final=True))

    def _filter(self, text: str) -> str:
        output: list[str] = []
        for character in text:
            if self._state == "text":
                self._consume_text(character, output)
            elif self._state == "escape":
                self._consume_escape(character)
            elif self._state == "escape_intermediate":
                self._consume_escape_intermediate(character)
            elif self._state == "csi":
                self._consume_csi(character)
            else:
                self._consume_string(character)
        return "".join(output)

    def _consume_text(self, character: str, output: list[str]) -> None:
        if character == "\x1b":
            self._state = "escape"
        elif character == "\x9b":
            self._state = "csi"
        elif character in self._OSC_INTRODUCERS:
            self._state = "osc"
            self._string_escape = False
        elif character in self._STRING_C1_INTRODUCERS:
            self._state = "string"
            self._string_escape = False
        elif character == "\x07":
            return
        elif character in {"\r", "\n", "\t", "\b"}:
            output.append(character)
        elif _is_printable(character):
            output.append(character)

    def _consume_escape(self, character: str) -> None:
        if character == "[" or character == "\x9b":
            self._state = "csi"
        elif character in self._OSC_INTRODUCERS:
            self._state = "osc"
            self._string_escape = False
        elif character in self._STRING_INTRODUCERS or character in self._STRING_C1_INTRODUCERS:
            self._state = "string"
            self._string_escape = False
        elif character == "\x1b":
            self._state = "escape"
        elif 0x20 <= ord(character) <= 0x2F:
            self._state = "escape_intermediate"
        else:
            self._state = "text"

    def _consume_escape_intermediate(self, character: str) -> None:
        codepoint = ord(character)
        if 0x30 <= codepoint <= 0x7E:
            self._state = "text"
        elif character == "\x1b":
            self._state = "escape"

    def _consume_csi(self, character: str) -> None:
        codepoint = ord(character)
        if 0x40 <= codepoint <= 0x7E:
            self._state = "text"
        elif character == "\x1b":
            self._state = "escape"

    def _consume_string(self, character: str) -> None:
        if self._state == "osc" and character == "\x07":
            self._state = "text"
            self._string_escape = False
        elif character == "\x9c":
            self._state = "text"
            self._string_escape = False
        elif self._string_escape:
            self._string_escape = False
            if character == "\\":
                self._state = "text"
            elif character == "\x1b":
                self._string_escape = True
        elif character == "\x1b":
            self._string_escape = True


def _is_printable(character: str) -> bool:
    """判断字符是否为普通文本而非 C0/C1 控制码。"""

    codepoint = ord(character)
    return codepoint >= 0x20 and not 0x7F <= codepoint <= 0x9F
