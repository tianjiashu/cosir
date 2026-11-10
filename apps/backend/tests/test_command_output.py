"""``CommandOutput``：一条命令输出的唯一收集点（卡片窗口、模型头尾、运行中读取、日志落盘）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.config.constant import Constant
from app.tools.tool_handler.terminal import command_output as command_output_module
from app.tools.tool_handler.terminal.command_output import CommandOutput


def _output(tmp_path: Path, **hooks: Any) -> CommandOutput:
    return CommandOutput(workspace_root=tmp_path, **hooks)


def test_short_output_keeps_raw_display_and_normalized_model_text(tmp_path: Path) -> None:
    raw = "\x1b[31mhello\x1b[0m\nworld\n"
    output = _output(tmp_path)

    output.feed(raw.encode())
    output.finish()

    summary = output.summary()
    assert summary.display == raw
    assert summary.model_head == "hello\nworld\n"
    assert summary.overflowed is False
    assert summary.log_path == ""
    assert summary.display_omitted_lines == 0
    assert not (tmp_path / ".cosir" / "tool-artifacts").exists()


def test_over_budget_writes_the_normalized_full_text_to_a_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Constant.Tools, "MAX_OUTPUT_CHARS", 50)
    raw = "a\n" * 100
    output = _output(tmp_path)

    output.feed(raw.encode())
    output.finish()

    summary = output.summary()
    assert summary.overflowed is True
    assert summary.model_total_chars == len(raw)
    assert summary.model_total_lines == 100
    assert summary.model_head == raw[:50]
    assert raw.endswith(summary.model_tail) and 0 < len(summary.model_tail) <= 50
    assert summary.log_path.startswith(".cosir/tool-artifacts/")
    assert (tmp_path / summary.log_path).read_text(encoding="utf-8") == raw


def test_card_display_is_trimmed_to_the_recent_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Constant.Terminal, "DISPLAY_OUTPUT_WINDOW_CHARS", 20)
    raw = "line\n" * 20
    output = _output(tmp_path)

    output.feed(raw.encode())
    output.finish()

    summary = output.summary()
    assert summary.display == "line\n" * 3
    assert len(summary.display) <= 25
    assert summary.display_omitted_lines == 17


def test_hooks_receive_raw_chunks_and_normalized_lines(tmp_path: Path) -> None:
    emitted: list[str] = []
    line_batches: list[tuple[list[str], str]] = []
    output = _output(
        tmp_path,
        on_text=emitted.append,
        on_lines=lambda lines, pending: line_batches.append((lines, pending)),
    )

    for chunk in [b"a\nb", b"\nc", b"\n"]:
        output.feed(chunk)
    output.finish()

    assert "".join(emitted) == "a\nb\nc\n"
    joined = "".join(line + "\n" for lines, _ in line_batches for line in lines)
    assert joined == output.summary().model_head
    assert line_batches[-1][1] == ""


def test_finish_is_idempotent_and_late_chunks_are_discarded(tmp_path: Path) -> None:
    emitted: list[str] = []
    output = _output(tmp_path, on_text=emitted.append)
    output.feed(b"starting now\n")
    output.finish()
    output.feed(b"finished\n")
    output.finish()

    assert emitted == ["starting now\n"]
    assert output.summary().display == "starting now\n"


def test_summary_before_finish_is_a_defect(tmp_path: Path) -> None:
    output = _output(tmp_path)
    with pytest.raises(RuntimeError):
        output.summary()


def test_log_write_failure_does_not_break_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Constant.Tools, "MAX_OUTPUT_CHARS", 50)

    def failing_open(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(command_output_module, "open", failing_open, raising=False)
    raw = "a\n" * 100
    output = _output(tmp_path)

    output.feed(raw.encode())
    output.finish()

    summary = output.summary()
    assert summary.overflowed is True
    assert summary.log_path == ""
    assert summary.model_head == raw[:50]
    assert raw.endswith(summary.model_tail)


def test_multibyte_characters_split_across_chunks_decode_correctly(tmp_path: Path) -> None:
    output = _output(tmp_path)
    output.feed(b"hello \xe4")
    output.feed(b"\xbd\xa0")
    output.finish()

    summary = output.summary()
    assert summary.display == "hello 你"
    assert summary.model_head == "hello 你\n"
