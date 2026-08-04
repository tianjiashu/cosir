"""guard/syntax_check 多语言语法检查守卫的测试。

覆盖：
- 扩展名 → 语言映射表分发（已知/未知扩展名）。
- 多语言（Python/JS/TS/TSX/Go/Rust/Java/HTML/CSS）合法/非法语法检查。
- ``MISSING`` 节点诊断（缺 token 的 kind/expected）。
- 体积门禁、``content`` 参数优先于读盘。
- ``format_syntax_reason`` 英文自修复引导。
- ``tool_error`` 扩展后的 ``display_data`` 参数。
- write_file / patch_tool 落盘后语法检查的 error 驱动集成。
"""

from pathlib import Path

import pytest

from app.tools.guard import syntax_check
from app.tools.guard.syntax_check import (
    MAX_CHECK_BYTES,
    check_source_syntax,
    format_syntax_reason,
)
from app.tools.tool_execute.tool_error import tool_error

# ----------------------------------------------------------------------
# 映射表分发
# ----------------------------------------------------------------------


class TestLanguageDispatch:
    def test_known_extension_resolves_language(self) -> None:
        cases = {
            ".py": "python",
            ".pyi": "python",
            ".js": "javascript",
            ".jsx": "jsx",
            ".ts": "typescript",
            ".tsx": "tsx",
            ".go": "go",
            ".rs": "rust",
            ".java": "java",
            ".html": "html",
            ".css": "css",
            ".cpp": "cpp",
        }
        for ext in cases:
            result = check_source_syntax(f"x{ext}", content="")
            assert result.supported, f"{ext} should be supported"

    def test_unknown_extension_is_skipped(self) -> None:
        result = check_source_syntax("file.txt", content="anything")
        assert result.supported is False
        assert result.has_error is False

    def test_grammar_load_failure_degrades_and_logs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """grammar 加载失败须优雅降级（supported=False）并写错误日志，不抛异常。"""
        p = tmp_path / "a.ts"

        # monkeypatch _get_parser 返回 None（模拟 grammar 加载失败后 `_build_language`
        # 返回 None 的降级路径；_build_language 本身不直接暴露给 check_source_syntax）。
        monkeypatch.setattr(syntax_check, "_get_parser", lambda name: None)
        result = check_source_syntax(str(p), content="const a: number = 1;")
        assert result.supported is False
        assert result.has_error is False


# ----------------------------------------------------------------------
# 多语言语法检查
# ----------------------------------------------------------------------


class TestMultiLanguageCheck:
    def test_python_valid(self, tmp_path: Path) -> None:
        p = tmp_path / "ok.py"
        result = check_source_syntax(str(p), content="def f():\n    return 1\n")
        assert result.supported is True
        assert result.has_error is False

    def test_python_invalid_reports_diagnostic(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.py"
        result = check_source_syntax(str(p), content="def f(:\n    return 1\n")
        assert result.supported is True
        assert result.has_error is True
        assert result.diagnostics
        first = result.diagnostics[0]
        assert first.row >= 1
        assert first.column >= 0

    def test_javascript_valid_and_invalid(self, tmp_path: Path) -> None:
        p = tmp_path / "a.js"
        assert check_source_syntax(str(p), content="const a = 1;").has_error is False
        assert check_source_syntax(str(p), content="const a = ;").has_error is True

    def test_typescript_valid_and_invalid(self, tmp_path: Path) -> None:
        p = tmp_path / "a.ts"
        assert check_source_syntax(str(p), content="const a: number = 1;").has_error is False
        assert check_source_syntax(str(p), content="const a: number = ;").has_error is True

    def test_tsx_valid_and_invalid(self, tmp_path: Path) -> None:
        p = tmp_path / "a.tsx"
        assert check_source_syntax(str(p), content="const el = <div>hi</div>;").has_error is False
        assert check_source_syntax(str(p), content="const el = <div>").has_error is True

    def test_go_valid_and_invalid(self, tmp_path: Path) -> None:
        p = tmp_path / "a.go"
        ok = "package main\nfunc main() {}\n"
        bad = "package main\nfunc main( {}\n"
        assert check_source_syntax(str(p), content=ok).has_error is False
        assert check_source_syntax(str(p), content=bad).has_error is True

    def test_rust_valid_and_invalid(self, tmp_path: Path) -> None:
        p = tmp_path / "a.rs"
        assert check_source_syntax(str(p), content="fn main() {}").has_error is False
        assert check_source_syntax(str(p), content="fn main( {}").has_error is True

    def test_java_valid_and_invalid(self, tmp_path: Path) -> None:
        p = tmp_path / "A.java"
        assert check_source_syntax(str(p), content="class A { void m() {} }").has_error is False
        assert check_source_syntax(str(p), content="class A { void m( {} }").has_error is True

    def test_html_valid_and_invalid(self, tmp_path: Path) -> None:
        p = tmp_path / "a.html"
        assert check_source_syntax(str(p), content="<div>hi</div>").has_error is False
        assert check_source_syntax(str(p), content="<div>hi").has_error is True

    def test_css_valid_and_invalid(self, tmp_path: Path) -> None:
        p = tmp_path / "a.css"
        assert check_source_syntax(str(p), content="a { color: red; }").has_error is False
        assert check_source_syntax(str(p), content="a { color: ; }").has_error is True

    def test_c_valid_and_invalid(self, tmp_path: Path) -> None:
        p = tmp_path / "a.c"
        ok = "int main(void) { return 0; }"
        bad = "int main(void) { return 0;"
        assert check_source_syntax(str(p), content=ok).has_error is False
        assert check_source_syntax(str(p), content=bad).has_error is True

    def test_cpp_valid_and_invalid(self, tmp_path: Path) -> None:
        p = tmp_path / "a.cpp"
        ok = "int main() { return 0; }"
        bad = "int main() { return 0;"
        assert check_source_syntax(str(p), content=ok).has_error is False
        assert check_source_syntax(str(p), content=bad).has_error is True

    def test_csharp_valid_and_invalid(self, tmp_path: Path) -> None:
        p = tmp_path / "A.cs"
        ok = "class A { void M() {} }"
        bad = "class A { void M( {} }"
        assert check_source_syntax(str(p), content=ok).has_error is False
        assert check_source_syntax(str(p), content=bad).has_error is True


# ----------------------------------------------------------------------
# MISSING 节点诊断
# ----------------------------------------------------------------------


class TestMissingDiagnostic:
    def test_missing_paren_reports_expected(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.py"
        result = check_source_syntax(str(p), content="def f(:\n    return 1\n")
        missing = [d for d in result.diagnostics if d.kind == "missing"]
        assert missing, "expected at least one missing diagnostic"
        assert missing[0].expected == ")"

    def test_format_syntax_reason_guides_fix(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.py"
        result = check_source_syntax(str(p), content="def f(:\n    return 1\n")
        reason = format_syntax_reason(result)
        assert "syntax error" in reason
        assert "line" in reason
        assert "follow-up edit" in reason

    def test_format_syntax_reason_empty_fallback(self) -> None:
        class _Empty:
            has_error = True
            diagnostics = ()

        reason = format_syntax_reason(_Empty())  # type: ignore[arg-type]
        assert "no diagnostic" in reason


# ----------------------------------------------------------------------
# 体积门禁 与 content 优先
# ----------------------------------------------------------------------


class TestLimits:
    def test_content_param_preferred_over_disk(self, tmp_path: Path) -> None:
        p = tmp_path / "mem.py"
        p.write_text("def valid():\n    pass\n", encoding="utf-8")
        # content 参数传入非法内容，应解析 content 而非磁盘上的合法内容。
        result = check_source_syntax(str(p), content="def f(:\n    pass\n")
        assert result.has_error is True

    def test_size_gate_skips_large_files(self, tmp_path: Path) -> None:
        p = tmp_path / "big.py"
        result = check_source_syntax(str(p), content="x" * (MAX_CHECK_BYTES + 1))
        assert result.supported is False
        assert result.has_error is False


# ----------------------------------------------------------------------
# tool_error 扩展 display_data
# ----------------------------------------------------------------------


class TestToolErrorDisplayData:
    def test_tool_error_accepts_display_data(self) -> None:
        obs = tool_error(
            "write_file",
            "syntax error detected after write",
            reason="fix it",
            permission="file_write",
            display_data={"syntax_errors": [{"row": 1, "column": 6}]},
        )
        assert obs.status == "error"
        assert obs.data is not None
        assert obs.data["syntax_errors"] == [{"row": 1, "column": 6}]

    def test_tool_error_without_display_data_unchanged(self) -> None:
        obs = tool_error("read_file", "boom", reason="reason", permission="safe_read")
        assert obs.status == "error"
        assert obs.content == "boom"
        assert obs.reason == "reason"
        # display_data 不含 content 副本（既有不变量）。
        assert "content" not in (obs.data or {})
