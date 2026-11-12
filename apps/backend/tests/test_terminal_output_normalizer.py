"""``OutputNormalizer``：把带控制序列与 ``\\r`` 进度条的命令输出整理成给模型的纯文本行。"""

from __future__ import annotations

import random

import pytest

from app.config.constant import Constant
from app.tools.terminal_runtime.command.output_normalizer import OutputNormalizer


def _normalize(chunks: list[str]) -> list[str]:
    normalizer = OutputNormalizer()
    lines: list[str] = []
    for chunk in chunks:
        lines.extend(normalizer.feed(chunk))
    lines.extend(normalizer.flush())
    return lines


def test_color_sequences_are_removed() -> None:
    assert _normalize(["\x1b[31mred\x1b[0m\n"]) == ["red"]


def test_carriage_return_overwrites_the_line() -> None:
    assert _normalize(["10%\r20%\r100%\n"]) == ["100%"]
    assert _normalize(["abc\rX\n"]) == ["Xbc"]
    assert _normalize(["abc\r\n"]) == ["abc"]


def test_backspace_moves_the_cursor_without_deleting() -> None:
    assert _normalize(["ab\bc\n"]) == ["ac"]


def test_line_erase_truncates_after_the_cursor() -> None:
    assert _normalize(["abcdef\r\x1b[K12\n"]) == ["12"]
    # 1K：把光标之前清成空格（光标停在行尾），随后写入落在后面。
    assert _normalize(["abc\x1b[1KX\n"]) == ["   X"]
    # 2K：清空整行，光标不动。
    assert _normalize(["abcdef\x1b[2K\r12\n"]) == ["12"]
    # 1K 含光标所在格：光标在 d 上时 a-d 都被清掉。
    assert _normalize(["abcdef\b\b\b\x1b[1K\n"]) == ["    ef"]


def test_horizontal_cursor_moves_rewrite_the_line() -> None:
    # Node readline 的刷新写法：回到第 1 列、清行、再写下一帧。
    assert _normalize(["frame1\x1b[1G\x1b[0Kframe2\n"]) == ["frame2"]
    assert _normalize(["frame1\x1b[2K\x1b[1Gframe2\n"]) == ["frame2"]
    assert _normalize(["abcdef\x1b[3DX\n"]) == ["abcXef"]
    assert _normalize(["ab\x1b[2CX\n"]) == ["ab  X"]
    # 私有前缀的序列不是定位。
    assert _normalize(["ab\x1b[?25lc\n"]) == ["abc"]


@pytest.mark.parametrize(
    "text",
    [
        "\x1b]8;;https://example.com\x1b\\link\x1b]8;;\x1b\\\n",
        "\x1b]0;title\x07link\n",
    ],
)
def test_osc_sequences_keep_only_their_text(text: str) -> None:
    """OSC 8 超链接与窗口标题整段丢弃，只留下中间看得见的文字。"""
    assert _normalize([text]) == ["link"]


def test_control_sequences_can_span_chunks() -> None:
    normalizer = OutputNormalizer()
    assert normalizer.feed("a\x1b[") == []
    assert normalizer.feed("31mb\n") == ["ab"]


def test_flush_returns_the_pending_line_and_pending_line_does_not_consume() -> None:
    normalizer = OutputNormalizer()
    assert normalizer.feed("Listening on :3000") == []
    assert normalizer.pending_line() == "Listening on :3000"
    assert normalizer.pending_line() == "Listening on :3000"
    assert normalizer.flush() == ["Listening on :3000"]
    assert normalizer.pending_line() == ""


def test_tabs_are_kept_and_other_controls_are_dropped() -> None:
    assert _normalize(["a\tb\x07c\r\n"]) == ["a\tbc"]
    assert _normalize(["\x9b31mred\n"]) == ["red"]


def test_overlong_line_is_truncated_with_a_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Constant.Terminal, "OBSERVATION_LINE_MAX_CHARS", 2)
    lines = _normalize(["x" * 100 + "\n"])
    assert lines == ["x" * 8 + " …[line truncated]"]


def test_random_chunking_matches_one_shot_normalization() -> None:
    text = (
        "\x1b[32mstarting\x1b[0m\n"
        "progress 10%\rprogress 100%\n"
        "\x1b]0;title\x07done\n"
        "a" * 50 + "\n"
        "tail"
    )
    expected = _normalize([text])
    rng = random.Random(7)  # noqa: S311 - 只用于切块复现，不涉及安全
    for _ in range(20):
        chunks: list[str] = []
        index = 0
        while index < len(text):
            size = rng.randint(1, 8)
            chunks.append(text[index : index + size])
            index += size
        assert _normalize(chunks) == expected
