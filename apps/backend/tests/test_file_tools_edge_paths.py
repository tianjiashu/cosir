"""Edge/error-path coverage for ``move_tool`` and ``file_operation_paths``.

These two modules carried the least coverage among the docstring-i18n target set.
The cases below deliberately drive the *failure* and boundary branches: empty and
non-string paths, workspace escapes, NUL bytes, non-UTF-8 / binary payloads, unknown
targets, cancellation, the bounded text-sampling window, and the POSIX-shaped
compensation branch of ``_move_without_overwriting``.  They assert the model-facing
``error``/``reason`` text stays English (ASCII) as well, because that text is part of
the contract under review.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.tools.schemas import ToolExecutionContext
from app.core.tools.tool_handler.move_tool import (
    MOVE_FILE_DESCRIPTION,
    MoveTool,
    _move_without_overwriting,
)
from app.core.tools.tool_handler.security.file_operation_paths import (
    ensure_utf8_text_file,
    resolve_workspace_relative_path,
)
from app.core.tools.tool_handler.security.path_resolver import PathResolver


def _context(root: Path, run_id: int = 4242) -> ToolExecutionContext:
    return ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=root, run_id=run_id)


# --- resolve_workspace_relative_path -------------------------------------------


# Empty / whitespace / non-string values must be rejected with an English message
# naming the offending argument (guards against a silently-accepted blank path).
@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_resolve_rejects_blank_paths(tmp_path: Path, value: str) -> None:
    resolver = PathResolver(tmp_path)

    resolved, error = resolve_workspace_relative_path(resolver, value, label="source_path")

    assert resolved is None
    assert error == "source_path must be a non-empty workspace-relative path"
    assert error.isascii()


def test_resolve_rejects_non_string_path(tmp_path: Path) -> None:
    resolver = PathResolver(tmp_path)

    resolved, error = resolve_workspace_relative_path(resolver, None, label="path")  # type: ignore[arg-type]

    assert resolved is None
    assert error == "path must be a non-empty workspace-relative path"


# Absolute POSIX paths, drive-letter paths, NUL bytes and ``.``/``..`` segments must
# all be reported as "not a normalized workspace-relative path".
@pytest.mark.parametrize(
    "value",
    ["/etc/passwd", "C:windows", "a\x00b", "./a.txt", "a/../b.txt", "a//b.txt"],
)
def test_resolve_rejects_unnormalized_paths(tmp_path: Path, value: str) -> None:
    resolver = PathResolver(tmp_path)

    resolved, error = resolve_workspace_relative_path(resolver, value, label="path")

    assert resolved is None
    assert error == "path must be a normalized workspace-relative path"
    assert error.isascii()


# A traversal path (``../x``) contains a ``..`` segment, so it is caught by the
# normalization guard before the containment check ever runs.
def test_resolve_rejects_traversal_segment(tmp_path: Path) -> None:
    resolver = PathResolver(tmp_path)

    resolved, error = resolve_workspace_relative_path(resolver, "../outside.txt", label="path")

    assert resolved is None
    assert "normalized workspace-relative path" in error


# A final-component symbolic link must be refused *as a link*, not followed, so the
# tool never deletes/moves the link target by accident.
def test_resolve_rejects_final_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("content\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("platform does not permit creating file symbolic links")
    resolver = PathResolver(tmp_path)

    resolved, error = resolve_workspace_relative_path(resolver, "link.txt", label="path")

    assert resolved is None
    assert error == "path must refer to a regular file, not a symbolic link"
    assert error.isascii()


# A device name must be blocked with an English reason that never leaks a Chinese
# translation (the shared ``blocked_device_reason`` text).
def test_resolve_blocks_device_name(tmp_path: Path) -> None:
    resolver = PathResolver(tmp_path)

    resolved, error = resolve_workspace_relative_path(resolver, "NUL", label="path")

    assert resolved is None
    assert error.isascii()
    assert "device" in error.lower() or "NUL" in error


# A valid nested path resolves to an absolute path inside the workspace root and
# yields an empty error string.
def test_resolve_accepts_nested_relative_path(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "file.txt"
    target.write_text("hi\n", encoding="utf-8")
    resolver = PathResolver(tmp_path)

    resolved, error = resolve_workspace_relative_path(resolver, "src/file.txt", label="path")

    assert error == ""
    assert resolved == target.resolve()


# Backslash separators must be normalized to forward slashes and still resolve.
def test_resolve_accepts_backslash_separators(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "file.txt").write_text("hi\n", encoding="utf-8")
    resolver = PathResolver(tmp_path)

    resolved, error = resolve_workspace_relative_path(resolver, "src\\file.txt", label="path")

    assert error == ""
    assert resolved == (tmp_path / "src" / "file.txt").resolve()


# --- ensure_utf8_text_file（有界采样）-------------------------------------------


def test_text_sample_rejects_missing_file_with_label(tmp_path: Path) -> None:
    error = ensure_utf8_text_file(tmp_path / "nope.txt", label="delete target")

    assert error == "delete target is not an existing regular file"
    assert error.isascii()


def test_text_sample_rejects_directory(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()

    assert ensure_utf8_text_file(folder, label="move source") == (
        "move source is not an existing regular file"
    )


# Non-UTF-8 bytes must be rejected with the label embedded in an English message.
def test_text_sample_rejects_non_utf8_content(tmp_path: Path) -> None:
    binary = tmp_path / "latin1.txt"
    binary.write_bytes(b"\xff\xfe invalid utf-8")

    error = ensure_utf8_text_file(binary, label="path")

    assert error.startswith("path is not a readable UTF-8 text file: ")
    assert error.isascii()


# A UTF-8 decodable file containing a NUL byte is treated as binary.  The bytes are
# written through ``os.open`` because ``Path.write_bytes`` on Windows truncates the
# payload at the first NUL and produces an empty (non-regular) file.
def test_text_sample_rejects_nul_bytes(tmp_path: Path) -> None:
    weird = tmp_path / "nul.txt"
    fd = os.open(weird, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_BINARY)
    try:
        os.write(fd, b"has\x00nul")
    finally:
        os.close(fd)
    if weird.stat().st_size == 0:
        pytest.skip("platform filesystem drops NUL payloads, cannot exercise the branch")

    error = ensure_utf8_text_file(weird, label="path")

    assert error == "path is binary; only UTF-8 text files are supported"
    assert error.isascii()


def test_text_sample_accepts_utf8_text(tmp_path: Path) -> None:
    good = tmp_path / "good.txt"
    good.write_bytes(b"hello\n")

    assert ensure_utf8_text_file(good, label="path") == ""


# 采样窗口之外的字节不参与判定：这正是「不整读文件」的实现证据（旧实现会拒绝该文件）。
def test_text_sample_ignores_bytes_outside_the_sample_window(tmp_path: Path) -> None:
    huge = tmp_path / "huge.txt"
    huge.write_bytes(b"text\n" * 10 + b"\x00\xff" + b"tail\n" * 10)

    assert ensure_utf8_text_file(huge, label="path", max_bytes=32) == ""


# 采样窗口恰好截断一个多字节字符时不得误判成「非法 UTF-8」。
def test_text_sample_tolerates_a_truncated_multibyte_tail(tmp_path: Path) -> None:
    cut = tmp_path / "cut.txt"
    cut.write_bytes("中".encode() + b"tail")

    assert ensure_utf8_text_file(cut, label="path", max_bytes=1) == ""


# 与上一条相反：文件真实结尾处就是截断的多字节序列时必须继续拒绝（采样不得放宽既有校验）。
def test_text_sample_rejects_a_truncated_multibyte_tail_at_eof(tmp_path: Path) -> None:
    cut = tmp_path / "eof-cut.txt"
    cut.write_bytes(b"caf\xe9")

    error = ensure_utf8_text_file(cut, label="path")

    assert error.startswith("path is not a readable UTF-8 text file: ")
    assert error.isascii()


def test_text_sample_requires_a_positive_window(tmp_path: Path) -> None:
    good = tmp_path / "good.txt"
    good.write_bytes(b"hello\n")

    with pytest.raises(ValueError):
        ensure_utf8_text_file(good, label="path", max_bytes=0)


# --- MoveTool error paths ------------------------------------------------------


# Missing source file: error must name the source and stay ASCII/English.
def test_move_missing_source_reports_english_error(tmp_path: Path) -> None:
    observation = MoveTool().execute(
        _context(tmp_path), source_path="missing.txt", destination_path="dest.txt"
    )

    assert observation.status == "error"
    assert observation.retryable is False
    assert observation.error == "move source is not an existing regular file"
    assert observation.error.isascii()
    assert observation.reason.isascii()


# Source resolving to the same file as destination must be rejected *before* any
# directory mutation.
def test_move_same_source_and_destination_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "same.txt"
    source.write_bytes(b"keep\n")

    observation = MoveTool().execute(
        _context(tmp_path), source_path="same.txt", destination_path="same.txt"
    )

    assert observation.status == "error"
    assert observation.error == "source and destination resolve to the same file"
    assert source.read_bytes() == b"keep\n"


# Destination that already exists (even as a symlink) must be refused and the source
# left untouched.
def test_move_existing_destination_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "s.txt"
    source.write_bytes(b"s\n")
    (tmp_path / "d.txt").write_bytes(b"d\n")

    observation = MoveTool().execute(
        _context(tmp_path), source_path="s.txt", destination_path="d.txt"
    )

    assert observation.status == "error"
    assert observation.error == "move destination already exists"
    assert (tmp_path / "d.txt").read_bytes() == b"d\n"
    assert source.exists()


# A missing destination parent directory must be refused (this tool never creates
# parents) with an English reason.
def test_move_missing_destination_parent_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "s.txt"
    source.write_text("s\n", encoding="utf-8")

    observation = MoveTool().execute(
        _context(tmp_path), source_path="s.txt", destination_path="nested/d.txt"
    )

    assert observation.status == "error"
    assert observation.error == "move destination parent directory does not exist"
    assert not (tmp_path / "nested").exists()
    assert source.exists()


# A cancelled run must short-circuit to ``cancelled`` and leave the filesystem
# untouched (no source removal, no destination creation).
def test_move_honours_run_cancellation(tmp_path: Path) -> None:
    source = tmp_path / "s.txt"
    source.write_text("s\n", encoding="utf-8")
    run_id = 7_700_001
    cancellation_registry.clear(run_id)
    context = _context(tmp_path, run_id=run_id)
    cancellation_registry.mark_cancelled(run_id)
    try:
        observation = MoveTool().execute(
            context, source_path="s.txt", destination_path="d.txt"
        )
    finally:
        cancellation_registry.clear(run_id)

    assert observation.status == "cancelled"
    assert source.exists()
    assert not (tmp_path / "d.txt").exists()


# --- _move_without_overwriting POSIX-shaped branch -----------------------------


def _os_proxy(monkeypatch, move_module, **overrides):
    """Install a stand-in ``os`` in ``move_tool`` without mutating the real ``os``.

    Patching the genuine ``os.name``/``os.unlink`` module attributes would leak into
    ``pathlib`` and pytest internals, so we swap the module *reference* instead.
    """

    class _Proxy:
        def __getattr__(self, item):
            if item in overrides:
                return overrides[item]
            return getattr(__import__("os"), item)

    monkeypatch.setattr(move_module, "os", _Proxy())


# Simulate POSIX semantics on any platform: ``os.link`` + ``os.unlink``.  The move
# must succeed and produce a real file at the destination with the same bytes.
def test_move_without_overwriting_link_unlink_success(tmp_path: Path, monkeypatch) -> None:
    import app.core.tools.tool_handler.move_tool as move_module

    source = tmp_path / "s.txt"
    source.write_bytes(b"payload\n")
    destination = tmp_path / "d.txt"
    _os_proxy(monkeypatch, move_module, name="posix")

    _move_without_overwriting(source, destination)

    assert not source.exists()
    assert destination.read_bytes() == b"payload\n"


# When the source unlink fails, the compensation must remove the freshly created
# destination link (still the same inode) and re-raise the original error so the
# caller can report a single, truthful failure.
def test_move_without_overwriting_unlink_failure_removes_destination(
    tmp_path: Path, monkeypatch
) -> None:
    import os as real_os

    import app.core.tools.tool_handler.move_tool as move_module

    source = tmp_path / "s.txt"
    source.write_bytes(b"payload\n")
    destination = tmp_path / "d.txt"

    def failing_unlink(path, *args, **kwargs):
        if Path(path) == source:
            raise OSError(errno.EACCES, "denied")
        return real_os.unlink(path, *args, **kwargs)

    _os_proxy(monkeypatch, move_module, name="posix", unlink=failing_unlink)

    with pytest.raises(OSError):
        _move_without_overwriting(source, destination)

    # Compensation removed the destination link; the source file survives.
    assert source.read_bytes() == b"payload\n"
    assert not destination.exists()


# The Windows branch simply forwards to ``os.rename`` and must not run the
# link/unlink path at all.
def test_move_without_overwriting_windows_branch_uses_rename(
    tmp_path: Path, monkeypatch
) -> None:
    import app.core.tools.tool_handler.move_tool as move_module

    source = tmp_path / "s.txt"
    source.write_bytes(b"win\n")
    destination = tmp_path / "d.txt"

    calls: list[tuple[object, object]] = []

    def fake_rename(src, dst):
        calls.append((src, dst))
        source.rename(destination)

    _os_proxy(monkeypatch, move_module, name="nt", rename=fake_rename)

    _move_without_overwriting(source, destination)

    assert calls == [(source, destination)]
    assert destination.read_bytes() == b"win\n"


# --- description contract (English, unchanged by the i18n pass) ----------------


# The registry description is model-facing and must remain English ASCII prose.
def test_move_description_is_english_ascii() -> None:
    assert MOVE_FILE_DESCRIPTION.isascii()
    assert "Move one existing UTF-8 text file" in MOVE_FILE_DESCRIPTION
    assert MoveTool.description == MOVE_FILE_DESCRIPTION
