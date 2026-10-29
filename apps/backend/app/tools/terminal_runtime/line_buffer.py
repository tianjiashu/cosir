"""带绝对行号的逻辑行缓冲：Agent 按行读取终端的唯一文本事实。

worker 交出两类物理行：滚出屏幕的 ``history``（软换行行不带 ``\\n``，硬换行行以 ``\\n`` 结尾）
与当前屏幕 ``lines``（每行带 ``wrapped``）。本模块把它们合并为逻辑行：

- 已封口区：history 拼接后按 ``\\n`` 切出的完整逻辑行，滚出即不可变，行号单调递增、永不复用；
  超过行数或字符上限时淘汰最旧行并推进 ``first_available_line``。
- 未封口段：history 末尾尚未遇到 ``\\n`` 的软换行文本，它的续行仍在屏幕顶部，因此与屏幕顶部
  第一条逻辑行拼接，不单独编号。
- 可变区：当前屏幕行按 ``wrapped`` 合并出的逻辑行，从 ``live_from_line`` 起编号；重绘、``\\r``
  覆盖、``clear`` 只改变可变区，已封口行号不受影响。

备用屏幕（vim、less 等）是独立画面，不进入逻辑行编号：进入时冻结主屏可变区（行号、光标保持
进入前的值），备用屏幕的物理行另存为「屏幕视图」（``screen_lines``，屏幕行号 1 基），退出时 worker
重发主屏，可变区照常重建。若备用屏幕行占用可变区行号，全屏程序重绘会让同一行号先后指向不同内容，
破坏「行号永不复用」——这是观察层把全屏程序单列的原因。worker 保证备用屏幕不产出滚动历史。

线程模型：不加锁，由所属会话锁串行访问；不做 IO。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from app.config.constant import Constant


@dataclass(frozen=True)
class LiveRegion:
    """一次屏幕快照投影出的可变区：逻辑行文本与光标所在逻辑行的下标（0 基）。"""

    lines: tuple[str, ...]
    cursor_index: int
    cursor_column: int


class TerminalLineBuffer:
    """已封口逻辑行 + 未封口段 + 当前屏幕可变区；行号 1 基。"""

    def __init__(self) -> None:
        self._sealed: deque[str] = deque()
        self._sealed_chars = 0
        self._first_line = 1
        self._pending = ""
        self._cursor_row = 0
        self.alternate = False
        self.rows = 0
        self.columns = 0
        self._live = LiveRegion((), 0, 0)
        self._screen: tuple[str, ...] = ()

    @property
    def first_available_line(self) -> int:
        return self._first_line

    @property
    def live_from_line(self) -> int:
        """可变区首行行号 = 已封口行总数 + 1（含已淘汰行）。"""
        return self._first_line + len(self._sealed)

    @property
    def last_line(self) -> int:
        """当前最后一条逻辑行的行号；缓冲为空时为 ``live_from_line - 1``。"""
        return self.live_from_line + len(self._live.lines) - 1

    @property
    def cursor_line(self) -> int:
        return self.live_from_line + self._live.cursor_index

    @property
    def cursor_column(self) -> int:
        return self._live.cursor_column

    @property
    def screen_cursor_row(self) -> int:
        """备用屏幕的光标屏幕行（1 基）；主屏时无意义。"""
        return self._cursor_row + 1

    def screen_lines(self) -> list[tuple[int, str]]:
        """备用屏幕视图：``(屏幕行号, 文本)``（``apply`` 时已裁掉光标以下的空白行）；主屏时为空。"""
        return list(enumerate(self._screen, start=1))

    def apply(self, screen: dict[str, Any], history: list[str]) -> None:
        """应用一次 worker ``screen`` 事件：先封口滚出行，再按主屏 / 备用屏幕更新对应视图。"""
        for chunk in history:
            self._append_history(chunk)
        cursor = screen["cursor"]
        self._cursor_row = cursor["row"]
        self.alternate = bool(screen["alternate"])
        self.rows = screen["rows"]
        self.columns = screen["columns"]
        if self.alternate:
            # 主屏可变区保持进入前的样子，行号与光标行不随全屏程序重绘变化。
            texts = [row["text"] for row in screen["lines"]]
            last = max(self._last_non_empty(texts), self._cursor_row)
            self._screen = tuple(texts[: last + 1])
            return
        self._screen = ()
        self._live = self._project_live(screen["lines"], cursor["column"])

    def initialize(self, rows: int, columns: int) -> None:
        """握手后、首个 screen 事件前的空屏状态。"""
        self.rows = rows
        self.columns = columns

    def _append_history(self, chunk: str) -> None:
        if not chunk:
            return
        text = self._pending + chunk
        parts = text.split("\n")
        self._pending = parts.pop()
        for line in parts:
            self._sealed.append(line)
            self._sealed_chars += len(line)
        while self._sealed and (
            len(self._sealed) > Constant.Terminal.MAX_BUFFER_LINES
            or self._sealed_chars > Constant.Terminal.MAX_BUFFER_CHARS
        ):
            self._sealed_chars -= len(self._sealed.popleft())
            self._first_line += 1

    def _project_live(self, rows: list[dict[str, Any]], screen_column: int) -> LiveRegion:
        lines: list[str] = []
        current = self._pending
        cursor_index = 0
        cursor_column = screen_column
        for index, row in enumerate(rows):
            if index == self._cursor_row:
                cursor_index = len(lines)
                # 光标列换算到逻辑行内：前面拼接的软换行文本都在光标左侧。
                cursor_column = len(current) + screen_column
            current += row["text"]
            if not row["wrapped"]:
                lines.append(current)
                current = ""
        if current:
            lines.append(current)
        last = max(self._last_non_empty(lines), cursor_index)
        return LiveRegion(tuple(lines[: last + 1]), cursor_index, cursor_column)

    @staticmethod
    def _last_non_empty(texts: list[str]) -> int:
        for index in range(len(texts) - 1, -1, -1):
            if texts[index].strip():
                return index
        return -1

    def get(self, start: int, end: int) -> list[tuple[int, str]]:
        """返回 ``[start, end]``（闭区间，自动夹到可用范围）的 ``(行号, 文本)``。"""
        start = max(start, self._first_line)
        end = min(end, self.last_line)
        result: list[tuple[int, str]] = []
        live_from = self.live_from_line
        for number in range(start, end + 1):
            if number < live_from:
                result.append((number, self._sealed[number - self._first_line]))
            else:
                result.append((number, self._live.lines[number - live_from]))
        return result
