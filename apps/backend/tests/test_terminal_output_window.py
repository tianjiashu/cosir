"""``trim_output_window``：卡片只保留最近一段原始输出，优先在整行边界上裁。"""

from __future__ import annotations

import pytest

from app.config.constant import Constant
from app.core.tool.terminal_output_window import trim_output_window


@pytest.fixture(autouse=True)
def _small_window(monkeypatch: pytest.MonkeyPatch) -> None:
    # 窗口 20：多留四分之一，超过 25 个字符才裁剪。
    monkeypatch.setattr(Constant.Terminal, "DISPLAY_OUTPUT_WINDOW_CHARS", 20)


def test_text_within_the_quarter_margin_is_returned_unchanged() -> None:
    text = "a" * 25
    assert trim_output_window(text) == (text, 0)


def test_overflowing_text_is_cut_at_a_line_boundary_and_counts_dropped_lines() -> None:
    kept, dropped = trim_output_window("line\n" * 20)
    assert kept == "line\n" * 3
    assert dropped == 17


def test_a_single_overlong_last_line_is_hard_cut_to_the_window() -> None:
    kept, dropped = trim_output_window("x" * 100)
    assert kept == "x" * 20
    assert dropped == 0
