"""逻辑行缓冲与观察文本渲染：行号单调、软换行合并、可变区、淘汰、备用屏幕与预算裁剪。"""

from __future__ import annotations

from typing import Any

import pytest

from app.config.constant import Constant
from app.tools.terminal_runtime.line_buffer import TerminalLineBuffer
from app.tools.terminal_runtime.render import render_lines


def screen(rows: list[Any], cursor: tuple[int, int] = (0, 0), alternate: bool = False) -> dict:
    lines = [
        {"text": r, "wrapped": False} if isinstance(r, str) else {"text": r[0], "wrapped": r[1]}
        for r in rows
    ]
    lines += [{"text": "", "wrapped": False}] * (5 - len(lines))
    return {
        "rows": 5,
        "columns": 10,
        "lines": lines,
        "cursor": {"row": cursor[0], "column": cursor[1]},
        "alternate": alternate,
    }


def texts(buffer: TerminalLineBuffer) -> list[tuple[int, str]]:
    return buffer.get(buffer.first_available_line, buffer.last_line)


def test_history_seals_logical_lines_with_monotonic_numbers() -> None:
    buffer = TerminalLineBuffer()
    buffer.apply(screen(["$ "], (0, 2)), ["one\n", "two-wrapped-", "part\n", "three\n"])
    assert texts(buffer) == [(1, "one"), (2, "two-wrapped-part"), (3, "three"), (4, "$ ")]
    assert buffer.live_from_line == 4
    assert buffer.cursor_line == 4
    buffer.apply(screen(["$ ls"], (0, 4)), ["four\n"])
    assert buffer.get(3, 4) == [(3, "three"), (4, "four")]
    assert buffer.live_from_line == 5


def test_unsealed_history_continues_into_screen_top() -> None:
    buffer = TerminalLineBuffer()
    buffer.apply(screen([("bbbbbbbbbb", True), "cc", "$ "], (2, 2)), ["aaaaaaaaaa"])
    assert texts(buffer) == [(1, "aaaaaaaaaabbbbbbbbbbcc"), (2, "$ ")]
    assert buffer.cursor_line == 2


def test_live_region_merges_wraps_trims_blank_bottom_and_maps_cursor_column() -> None:
    buffer = TerminalLineBuffer()
    buffer.apply(screen([("0123456789", True), "ab", "", "", ""], (1, 2)), [])
    assert texts(buffer) == [(1, "0123456789ab")]
    assert buffer.cursor_line == 1
    assert buffer.cursor_column == 12


def test_clear_keeps_numbers_continuous() -> None:
    buffer = TerminalLineBuffer()
    buffer.apply(screen(["a", "b", "$ clear"], (2, 7)), [])
    assert buffer.last_line == 3
    # clear 把可见内容推进历史（alacritty clear_viewport），屏幕只剩提示符。
    buffer.apply(screen(["$ "], (0, 2)), ["a\n", "b\n", "$ clear\n"])
    assert texts(buffer) == [(1, "a"), (2, "b"), (3, "$ clear"), (4, "$ ")]


def test_eviction_by_lines_and_chars_advances_first_available_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Constant.Terminal, "MAX_BUFFER_LINES", 3)
    buffer = TerminalLineBuffer()
    buffer.apply(screen(["$ "]), [f"l{i}\n" for i in range(1, 6)])
    assert buffer.first_available_line == 3
    assert [n for n, _ in texts(buffer)] == [3, 4, 5, 6]
    monkeypatch.setattr(Constant.Terminal, "MAX_BUFFER_LINES", 10_000)
    monkeypatch.setattr(Constant.Terminal, "MAX_BUFFER_CHARS", 10)
    chars = TerminalLineBuffer()
    chars.apply(screen(["$ "]), ["aaaaaa\n", "bbbbbb\n"])
    assert chars.first_available_line == 2
    assert chars.get(1, 1) == []


def test_alternate_screen_is_a_separate_view_and_freezes_line_numbers() -> None:
    buffer = TerminalLineBuffer()
    buffer.apply(screen(["$ echo a", "a", "$ less f"], (2, 8)), ["old\n"])
    before = texts(buffer)
    assert before == [(1, "old"), (2, "$ echo a"), (3, "a"), (4, "$ less f")]
    # 进入全屏程序并重绘两次：逻辑行与光标行保持进入前的样子，屏幕行另走一套编号。
    buffer.apply(screen([("row1", True), "row2", ":"], (2, 1), alternate=True), [])
    assert buffer.alternate
    assert texts(buffer) == before and buffer.cursor_line == 4
    assert buffer.screen_lines() == [(1, "row1"), (2, "row2"), (3, ":")]
    assert buffer.screen_cursor_row == 3
    buffer.apply(screen(["row4", "row5", ":"], (2, 1), alternate=True), [])
    assert texts(buffer) == before
    assert buffer.screen_lines() == [(1, "row4"), (2, "row5"), (3, ":")]
    # 退出后主屏重建，行号接着进入前的编号走，屏幕视图清空。
    buffer.apply(screen(["$ echo a", "a", "$ less f", "$ "], (3, 2)), [])
    assert not buffer.alternate and buffer.screen_lines() == []
    assert texts(buffer)[-1] == (5, "$ ")


def test_render_both_keeps_head_and_tail_and_marks_omitted_range() -> None:
    lines = [(n, f"line {n}") for n in range(1, 201)]
    rendered = render_lines(lines, "both", 100_000)
    assert rendered.omitted == (21, 140)
    assert rendered.body[0] == "1│line 1"
    assert rendered.body[20].startswith("--- omitted lines 21-140")
    assert rendered.body[-1] == "200│line 200"
    small = render_lines(lines[:50], "both", 100_000)
    assert small.omitted is None and len(small.body) == 50


def test_render_both_within_window_over_budget_keeps_the_tail() -> None:
    # 行数不超过头 20 + 尾 60 但总长超预算：尾部优先，最后一行必须保留，省略区间在中间。
    lines = [(n, "x" * 1500) for n in range(1, 41)]
    rendered = render_lines(lines, "both", 18_000)
    assert rendered.body[-1].startswith("40│")
    assert rendered.omitted is not None
    first_omitted, last_omitted = rendered.omitted
    assert 1 < first_omitted <= last_omitted < 40
    assert rendered.body[0].startswith("1│")
    assert sum(len(row) + 1 for row in rendered.body) <= 18_000 + 200
    # 预算放得下时原样全部返回，不制造省略。
    fits = render_lines(lines, "both", 100_000)
    assert fits.omitted is None and len(fits.body) == 40


def test_render_budget_trims_tail_mode_from_front_and_head_mode_from_back() -> None:
    lines = [(n, "x" * 50) for n in range(1, 101)]
    tail = render_lines(lines, "tail", 600)
    assert tail.last == 100 and tail.first is not None and tail.first >= 89
    head = render_lines(lines, "head", 600)
    assert head.first == 1 and head.truncated_after is not None
    both = render_lines(lines, "both", 1200)
    assert both.omitted is not None
    assert sum(len(row) + 1 for row in both.body) < 1400


def test_long_lines_are_clipped() -> None:
    limit = Constant.Terminal.OBSERVATION_LINE_MAX_CHARS
    rendered = render_lines([(1, "y" * (limit + 10))], "tail", 100_000)
    assert rendered.body[0].endswith("…(+10 chars)")


@pytest.mark.parametrize("mode", ["both", "tail", "head"])
def test_render_empty(mode: str) -> None:
    assert render_lines([], mode, 100).first is None  # type: ignore[arg-type]
