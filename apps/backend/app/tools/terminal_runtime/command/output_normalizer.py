"""终端命令输出的整理：把带 ANSI 控制序列与 ``\\r`` 进度条的解码文本整理成给模型读的纯文本行。

唯一调用方是同目录的 ``command_output``（解码 → 整理 → 收集），整理后的行供模型通道与运行中读取；
卡片展示保留原始文本、不经过这里。增量状态机：按块喂入，跨块的
半截控制序列与半行留在内部状态里，因此调用方可以按任意块边界喂入。

只做行内的水平定位（``\r``、退格、CSI ``G`` / ``C`` / ``D``）与行内擦除（CSI ``K``），不做光标上下
移动、颜色保留与 Unicode 宽度计算：管道输出没有屏幕几何，这里只保证「模型看到的一行是终端最终
显示的那一行文字」。列号按码点计，含宽字符的行里定位可能偏几格，可以接受。
"""

from __future__ import annotations

from app.config.constant import Constant

_ESC = "\x1b"
_TRUNCATION_MARK = " …[line truncated]"


def _max_line_chars() -> int:
    """单行可打印字符上限：观察渲染的单行上限的 4 倍。

    取常量而不是固定值，测试可 monkeypatch ``OBSERVATION_LINE_MAX_CHARS`` 调小。
    """
    return Constant.Terminal.OBSERVATION_LINE_MAX_CHARS * 4


class OutputNormalizer:
    """增量整理：按块喂入，跨块的半截控制序列与半行留在内部状态里。"""

    def __init__(self) -> None:
        self._chars: list[str] = []
        self._cursor = 0
        self._truncated = False
        # 控制序列状态机：normal / escape / escape_intermediate / csi / osc / osc_escape /
        # string / string_escape。半截序列跨块保存在这里。
        self._state = "normal"
        self._csi_params = ""

    def feed(self, text: str) -> list[str]:
        """喂入一块文本，返回这块里已完成（遇到 ``\\n``）的行，不含换行符。"""
        lines: list[str] = []
        for char in text:
            line = self._step(char)
            if line is not None:
                lines.append(line)
        return lines

    def pending_line(self) -> str:
        """当前还没换行的那一行（如服务打印的 ``Listening on :3000``），只读，不消费。"""
        return "".join(self._chars)

    def flush(self) -> list[str]:
        """输入结束：把未完成的行（非空时）作为最后一行返回，并清空状态。"""
        line = self._finish_line() if self._chars or self._truncated else ""
        self._state = "normal"
        self._csi_params = ""
        return [line] if line else []

    def _step(self, char: str) -> str | None:
        if self._state == "normal":
            return self._step_normal(char)
        if self._state == "escape":
            return self._step_escape(char)
        if self._state == "escape_intermediate":
            if 0x20 <= ord(char) <= 0x2F:
                return None
            self._state = "normal"
            return None
        if self._state == "csi":
            return self._step_csi(char)
        if self._state == "osc":
            if char == "\x07":
                self._state = "normal"
            elif char == _ESC:
                self._state = "osc_escape"
            return None
        if self._state == "osc_escape":
            if char == "\\":
                self._state = "normal"
            elif char != _ESC:
                self._state = "osc"
            return None
        if self._state == "string":
            if char == _ESC:
                self._state = "string_escape"
            return None
        # string_escape：DCS / SOS / PM / APC 以 ESC \ 结束。
        if char == "\\":
            self._state = "normal"
        elif char != _ESC:
            self._state = "string"
        return None

    def _step_normal(self, char: str) -> str | None:
        if char == "\n":
            return self._finish_line()
        if char == "\r":
            self._cursor = 0
            return None
        if char == "\b":
            self._cursor = max(0, self._cursor - 1)
            return None
        if char == _ESC:
            self._state = "escape"
            return None
        if char == "\x9b":
            self._state = "csi"
            self._csi_params = ""
            return None
        code = ord(char)
        if char == "\t" or (code >= 0x20 and code != 0x7F):
            self._write(char)
        return None

    def _step_escape(self, char: str) -> str | None:
        if char == "[":
            self._state = "csi"
            self._csi_params = ""
            return None
        if char == "]":
            self._state = "osc"
            return None
        if char in "PX^_":
            self._state = "string"
            return None
        if 0x20 <= ord(char) <= 0x2F:
            self._state = "escape_intermediate"
            return None
        # ESC + 其他单个字节：整段丢弃。
        self._state = "normal"
        return None

    def _step_csi(self, char: str) -> str | None:
        code = ord(char)
        if 0x30 <= code <= 0x3F:
            self._csi_params += char
            return None
        if 0x20 <= code <= 0x2F:
            return None
        self._state = "normal"
        if char == "K":
            self._erase_in_line()
        elif char in "GCD" and not self._csi_params.startswith(("?", ">", "<", "=")):
            self._move_cursor(char)
        return None

    def _move_cursor(self, final: str) -> None:
        """CSI ``G`` 移到第 n 列（1 起），``C`` / ``D`` 右移 / 左移 n 格；参数缺省或 0 按 1。"""
        first = self._csi_params.split(";", 1)[0]
        count = max(int(first), 1) if first.isdigit() else 1
        if final == "G":
            self._cursor = min(count - 1, _max_line_chars())
        elif final == "C":
            self._cursor = min(self._cursor + count, _max_line_chars())
        else:
            self._cursor = max(self._cursor - count, 0)

    def _write(self, char: str) -> None:
        if self._cursor >= _max_line_chars():
            # 没有换行的超长输出：多出的可打印字符丢弃，行完成时标记截断，避免撑爆内存。
            self._truncated = True
            return
        while len(self._chars) < self._cursor:
            self._chars.append(" ")
        if self._cursor < len(self._chars):
            self._chars[self._cursor] = char
        else:
            self._chars.append(char)
        self._cursor += 1

    def _erase_in_line(self) -> None:
        """CSI ``K``：行内擦除；参数空或 0 截断光标之后，1 把行首到光标（含）清成空格，2 清空整行。"""
        mode = self._csi_params.split(";", 1)[0]
        if mode in ("", "0"):
            del self._chars[self._cursor :]
        elif mode == "1":
            for index in range(min(self._cursor + 1, len(self._chars))):
                self._chars[index] = " "
        elif mode == "2":
            self._chars.clear()

    def _finish_line(self) -> str:
        line = "".join(self._chars).rstrip()
        if self._truncated:
            line += _TRUNCATION_MARK
        self._chars = []
        self._cursor = 0
        self._truncated = False
        return line
