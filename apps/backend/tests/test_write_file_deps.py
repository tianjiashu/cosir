"""write_file / patch 工具依赖模块的补充边界测试。

集中覆盖 atomic_write / syntax_check / patch_apply / patch_parser / patch_diff
中未被工具主流程走到、但属工具依赖方法的分支，验证其独立正确性与潜在 bug。
"""

import errno

import pytest

from app.tools.guard.syntax_check import (
    check_source_syntax,
    format_syntax_reason,
)
from app.tools.tool_handler.file_io.atomic_write import (
    atomic_write_text,
    detect_bom,
    detect_line_ending,
    looks_like_line_numbered,
)
from app.tools.tool_handler.patch.patch_apply import (
    PatchApplyError,
    apply_all,
    apply_all_with_diff,
    validate_all,
)
from app.tools.tool_handler.patch.patch_diff import (
    FileDiffResult,
    build_diff_stats,
    format_patch_diff,
    format_unified_diff,
)
from app.tools.tool_handler.patch.patch_parser import (
    OperationType,
    parse_v4a_patch,
)
from app.tools.tool_handler.security.path_resolver import PathResolver

# --------------------------------------------------------------------------- #
# atomic_write 边界
# --------------------------------------------------------------------------- #


def test_detect_line_ending():
    assert detect_line_ending("a\r\nb") == "\r\n"
    assert detect_line_ending("a\nb") == "\n"
    assert detect_line_ending("no newline") == "\n"


def test_detect_bom():
    assert detect_bom("\ufeffhi") == b"\xef\xbb\xbf"
    assert detect_bom("hi") is None


def test_looks_like_line_numbered_empty():
    assert looks_like_line_numbered("") is False


def test_looks_like_line_numbered_all_prefixed():
    assert looks_like_line_numbered("1| a\n2| b\n3| c") is True


def test_looks_like_line_numbered_none_prefixed():
    assert looks_like_line_numbered("plain\ntext\nhere") is False


def test_atomic_write_preserve_eol_false(tmp_path):
    target = tmp_path / "raw.txt"
    target.write_bytes(b"old\r\n")
    atomic_write_text(target, "a\nb\n", preserve_eol=False)
    # preserve_eol=False 原样写入，不套用既有 CRLF。
    assert target.read_bytes() == b"a\nb\n"


def test_atomic_write_containment_reject_escape(tmp_path):
    """containment_root 提供时，越界目标被拒并转 OSError EPERM。"""

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    with pytest.raises(OSError) as exc_info:
        atomic_write_text(outside, "x", containment_root=root)
    assert exc_info.value.errno == errno.EPERM


def test_atomic_write_temp_cleaned_on_write_failure(tmp_path, monkeypatch):
    """写入中途异常应清理临时文件，不留半写残骸。"""

    import app.tools.tool_handler.file_io.atomic_write as aw

    target = tmp_path / "f.txt"

    real_replace = aw.os.replace

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(aw.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(target, "data")
    # 临时文件不应遗留。
    leftovers = list(tmp_path.glob(".tmp_write_*"))
    assert leftovers == []
    monkeypatch.setattr(aw.os, "replace", real_replace)


# --------------------------------------------------------------------------- #
# syntax_check 边界
# --------------------------------------------------------------------------- #


def test_syntax_check_unknown_extension_unsupported():
    result = check_source_syntax("file.unknownext", "garbage :::")
    assert result.supported is False


def test_syntax_check_valid_python():
    result = check_source_syntax("ok.py", "x = 1\n")
    assert result.supported is True
    assert result.has_error is False


def test_syntax_check_invalid_python_has_diagnostics():
    result = check_source_syntax("bad.py", "def f(:\n")
    assert result.supported is True
    assert result.has_error is True
    assert len(result.diagnostics) >= 1


def test_syntax_check_oversize_skipped():
    big = "x = 1\n" * (2 * 1024 * 1024)  # > MAX_CHECK_BYTES
    result = check_source_syntax("big.py", big)
    assert result.supported is False


def test_syntax_check_read_from_disk(tmp_path):
    f = tmp_path / "disk.py"
    f.write_text("y = 2\n", encoding="utf-8")
    result = check_source_syntax(str(f))
    assert result.supported is True
    assert result.has_error is False


def test_syntax_check_read_missing_file_unsupported(tmp_path):
    result = check_source_syntax(str(tmp_path / "nope.py"))
    assert result.supported is False


def test_format_syntax_reason_no_error_fallback():
    from app.tools.guard.syntax_check import SyntaxCheckResult

    reason = format_syntax_reason(SyntaxCheckResult(supported=True, has_error=False))
    assert "no diagnostic detail" in reason


# --------------------------------------------------------------------------- #
# patch_diff
# --------------------------------------------------------------------------- #


def test_format_unified_diff_no_change():
    assert format_unified_diff("same", "same", "a.txt") == "(no textual change)"


def test_format_unified_diff_change():
    diff = format_unified_diff("a\n", "b\n", "f.txt")
    assert "a/f.txt" in diff
    assert "+b" in diff


def test_build_diff_stats_counts():
    results = [FileDiffResult(path="f.txt", status="modified", before="a\n", after="a\nb\n")]
    stats = build_diff_stats(results)
    assert stats["total_files"] == 1
    assert stats["total_insertions"] == 1
    assert stats["total_deletions"] == 0


def test_build_diff_stats_move_counts_zero():
    results = [
        FileDiffResult(path="a", status="moved", before="x", after="x", new_path="b")
    ]
    stats = build_diff_stats(results)
    assert stats["total_insertions"] == 0
    assert stats["total_deletions"] == 0


def test_format_patch_diff_move_marker():
    results = [
        FileDiffResult(path="a", status="moved", before="x", after="x", new_path="b")
    ]
    out = format_patch_diff(results)
    assert "# Moved: a -> b" in out


# --------------------------------------------------------------------------- #
# patch_apply 直接测试（含 apply_all 薄封装）
# --------------------------------------------------------------------------- #


def test_validate_all_update_hunk_not_found(tmp_path):
    (tmp_path / "u.txt").write_text("real content", encoding="utf-8")
    resolver = PathResolver(tmp_path)
    ops, _ = parse_v4a_patch(
        "*** Begin Patch\n*** Update File: u.txt\n@@\n-does not exist\n+new\n*** End Patch\n"
    )
    errors = validate_all(ops, resolver)
    assert any("not found" in e for e in errors)


def test_validate_all_empty_hunk(tmp_path):
    (tmp_path / "u.txt").write_text("content", encoding="utf-8")
    # 构造一个含空 hunk 的 UPDATE：@@ 后无任何 +/-/context 行。
    ops, err = parse_v4a_patch(
        "*** Begin Patch\n*** Update File: u.txt\n@@\n*** End Patch\n"
    )
    # 无 hunk 行会被解析器判为 no hunks（解析错误），故直接验证解析层。
    assert err is not None or ops == []


def test_apply_all_thin_wrapper(tmp_path):
    resolver = PathResolver(tmp_path)
    ops, _ = parse_v4a_patch(
        "*** Begin Patch\n*** Add File: new.txt\n+content\n*** End Patch\n"
    )
    # apply_all 返回 None（薄封装），落盘生效。
    assert apply_all(ops, resolver) is None
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "content"


def test_apply_all_with_diff_add(tmp_path):
    resolver = PathResolver(tmp_path)
    ops, _ = parse_v4a_patch(
        "*** Begin Patch\n*** Add File: a.txt\n+hi\n*** End Patch\n"
    )
    results = apply_all_with_diff(ops, resolver)
    assert results[0].status == "added"
    assert results[0].after == "hi"


def test_apply_all_with_diff_partial_then_error(tmp_path):
    """第一个 add 成功、第二个目标已存在 -> PatchApplyError.partial_applied=True。"""

    resolver = PathResolver(tmp_path)
    (tmp_path / "conflict.txt").write_text("exists", encoding="utf-8")
    # 绕过 validate_all，直接 apply：第一个成功、第二个 add 到已存在文件抛错。
    ops, _ = parse_v4a_patch(
        "*** Begin Patch\n"
        "*** Add File: first.txt\n+one\n"
        "*** Add File: conflict.txt\n+two\n"
        "*** End Patch\n"
    )
    with pytest.raises(PatchApplyError) as exc_info:
        apply_all_with_diff(ops, resolver)
    assert exc_info.value.partial_applied is True
    # 已知限制固化：第一个文件已落盘且不回滚。
    assert (tmp_path / "first.txt").exists()


# --------------------------------------------------------------------------- #
# patch_parser 边界
# --------------------------------------------------------------------------- #


def test_parse_delete_operation():
    ops, err = parse_v4a_patch(
        "*** Begin Patch\n*** Delete File: gone.txt\n*** End Patch\n"
    )
    assert err is None
    assert ops[0].operation == OperationType.DELETE


def test_parse_move_operation():
    ops, err = parse_v4a_patch(
        "*** Begin Patch\n*** Move File: a.txt -> b.txt\n*** End Patch\n"
    )
    assert err is None
    assert ops[0].operation == OperationType.MOVE
    assert ops[0].new_path == "b.txt"


def test_parse_add_hunk_lines_captured():
    ops, err = parse_v4a_patch(
        "*** Begin Patch\n*** Add File: a.txt\n+line1\n+line2\n*** End Patch\n"
    )
    assert err is None
    assert ops[0].operation == OperationType.ADD
    plus = [ln.content for h in ops[0].hunks for ln in h.lines if ln.prefix == "+"]
    assert plus == ["line1", "line2"]


def test_parse_update_without_hunks_is_error():
    ops, err = parse_v4a_patch(
        "*** Begin Patch\n*** Update File: a.txt\n*** End Patch\n"
    )
    assert err is not None
    assert "no hunks" in err
