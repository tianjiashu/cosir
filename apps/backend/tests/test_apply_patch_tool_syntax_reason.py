"""Regression guard for ``ApplyPatchTool._format_syntax_reason``.

Motivation: ``_format_syntax_reason`` renders post-write syntax diagnostics into the
model-facing ``content`` field.  Before this file it had **zero** test coverage, so a
mistranslation during the docstring-Chinese migration (or any future edit) of the
English model-channel text could ship silently.  The behaviour below is the contract
this guard freezes:

* empty diagnostic list  -> generic fix hint (still English),
* non-empty list         -> ASCII ``line <row> col <column>`` per diagnostic, with
  ``missing '<token>'`` when ``item.expected`` is set and ``unexpected token``
  otherwise.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.core.tools.guard.syntax_check import SyntaxDiagnostic
from app.core.tools.tool_handler.apply_patch_tool import ApplyPatchTool

import pytest


def _diagnostic(
    *,
    row: int,
    column: int,
    expected: str | None,
    language: str = "python",
) -> SyntaxDiagnostic:
    """Build a diagnostic; ``message`` mirrors the kernel's own rendering."""
    if expected is not None:
        kind = "missing"
        message = f"missing '{expected}' at line {row} col {column}"
    else:
        kind = "unexpected"
        message = f"unexpected token at line {row} col {column}"
    return SyntaxDiagnostic(
        language=language,
        row=row,
        column=column,
        kind=kind,
        message=message,
        expected=expected,
    )


# Empty diagnostics: must return the generic, still-English fallback hint (guards
# against an accidental Chinese translation of the model-channel text).
def test_empty_diagnostics_returns_generic_english_hint() -> None:
    reason = ApplyPatchTool._format_syntax_reason([])

    assert reason == (
        "the patched files have syntax errors; fix them with write_file or apply_patch."
    )
    assert reason.isascii(), "model-facing fallback hint must stay ASCII/English"


# Non-empty diagnostics with ``expected`` set: one ``line <row> col <column>
# missing '<token>'`` entry per diagnostic, joined by ``; ``.
def test_missing_token_diagnostics_render_line_col_and_missing_token() -> None:
    diagnostics = [
        _diagnostic(row=3, column=10, expected=")"),
        _diagnostic(row=8, column=0, expected=":"),
    ]

    reason = ApplyPatchTool._format_syntax_reason(diagnostics)

    assert "line 3 col 10 missing ')'" in reason
    assert "line 8 col 0 missing ':'" in reason
    assert "line 3 col 10 missing ')'; line 8 col 0 missing ':'" in reason
    assert reason.endswith("fix them with a new apply_patch or write_file call.")
    assert reason.isascii()


# Diagnostics without ``expected``: the branch must emit ``unexpected token`` and
# must NOT emit the ``missing`` form.
def test_unexpected_token_diagnostics_render_unexpected_token() -> None:
    diagnostics = [_diagnostic(row=1, column=4, expected=None)]

    reason = ApplyPatchTool._format_syntax_reason(diagnostics)

    assert "line 1 col 4 unexpected token" in reason
    assert "missing" not in reason
    assert reason.isascii()


# Mixed list: the per-item ternary must pick the right wording for each element,
# and the ordering must follow the input order (no re-sorting / dedup).
def test_mixed_diagnostics_keep_order_and_per_item_wording() -> None:
    diagnostics = [
        _diagnostic(row=2, column=7, expected=None),
        _diagnostic(row=5, column=1, expected="}"),
    ]

    reason = ApplyPatchTool._format_syntax_reason(diagnostics)

    assert "line 2 col 7 unexpected token" in reason
    assert "line 5 col 1 missing '}'" in reason
    assert reason.index("line 2 col 7") < reason.index("line 5 col 1")
    assert reason.count("unexpected token") == 1
    assert reason.count("missing '}'") == 1


# Boundary: row/column render verbatim, including 0 and large numbers, with no
# off-by-one adjustment applied by the formatter itself.
def test_numeric_bounds_are_rendered_verbatim() -> None:
    diagnostics = [
        _diagnostic(row=0, column=0, expected=None),
        _diagnostic(row=10**6, column=10**6, expected="TOKEN"),
    ]

    reason = ApplyPatchTool._format_syntax_reason(diagnostics)

    assert "line 0 col 0 unexpected token" in reason
    assert f"line {10**6} col {10**6} missing 'TOKEN'" in reason


# Source-level contract: every string literal returned by ``_format_syntax_reason``
# (i.e. the model-channel text of this function) must remain ASCII, so a future
# docstring translation cannot bleed into the returned text.
def test_returned_literals_in_source_are_ascii() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "core"
        / "tools"
        / "tool_handler"
        / "apply_patch_tool.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    formatter = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_format_syntax_reason"
    )
    # The leading expression statement is the (deliberately Chinese) docstring; it is
    # not part of the model channel, so exclude that node and keep only the literals
    # the function actually returns/concatenates.
    docstring_node = formatter.body[0].value if isinstance(formatter.body[0], ast.Expr) else None
    literals = [
        node.value
        for node in ast.walk(formatter)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node is not docstring_node
    ]

    assert literals, "expected at least one string literal in the formatter"
    for literal in literals:
        assert literal.isascii(), f"non-ASCII literal in model channel: {literal!r}"


# --- malformed-input robustness ------------------------------------------------

# Defensive check: the formatter consumes whatever the kernel produced.  An empty
# string ``expected`` is falsy, so it must take the ``unexpected token`` branch
# (not emit ``missing ''``), which is the truthful rendering for "no token known".
def test_empty_string_expected_uses_unexpected_branch() -> None:
    diagnostic = _diagnostic(row=4, column=2, expected="")

    reason = ApplyPatchTool._format_syntax_reason([diagnostic])

    assert "line 4 col 2 unexpected token" in reason
    assert "missing ''" not in reason


# Robustness: the formatter is a pure function and must not mutate the caller's
# diagnostic list (a hidden in-place sort/dedup would corrupt the observation).
def test_input_list_is_not_mutated() -> None:
    diagnostics = [
        _diagnostic(row=9, column=3, expected=None),
        _diagnostic(row=1, column=1, expected=")"),
    ]
    snapshot = list(diagnostics)

    ApplyPatchTool._format_syntax_reason(diagnostics)

    assert diagnostics == snapshot


# Determinism: identical inputs must yield byte-identical output.
def test_output_is_deterministic() -> None:
    diagnostics = [
        _diagnostic(row=1, column=1, expected=")"),
        _diagnostic(row=2, column=2, expected=None),
    ]

    assert ApplyPatchTool._format_syntax_reason(diagnostics) == (
        ApplyPatchTool._format_syntax_reason(diagnostics)
    )


# --- end-to-end: the success observation wires the formatter into ``content`` ------


# A successful patch that leaves an invalid ``.py`` file must still report
# ``success`` but carry English, ASCII syntax guidance in ``content``.
def test_success_with_broken_python_reports_english_syntax_content(tmp_path: Path) -> None:
    from app.core.tools.schemas import ToolExecutionContext

    target = tmp_path / "mod.py"
    target.write_bytes(b"def f(\n")
    patch = "\n".join(
        (
            "diff --git a/mod.py b/mod.py",
            "--- a/mod.py",
            "+++ b/mod.py",
            "@@ -1 +1 @@",
            "-def f(",
            "+def f(]",
        )
    )
    context = ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=tmp_path, run_id=1)

    observation = ApplyPatchTool().execute(context, patch=patch)

    assert observation.status == "success"
    assert observation.content is not None
    assert observation.content.startswith("success\nPost-write syntax check reported issues:\n")
    assert observation.content.isascii(), "model content must stay ASCII/English"


# A cancelled run must short-circuit to ``cancelled`` before any hunk is written.
def test_cancelled_run_returns_cancelled_without_writing(tmp_path: Path) -> None:
    from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
    from app.core.tools.schemas import ToolExecutionContext

    target = tmp_path / "a.txt"
    target.write_bytes(b"old\n")
    patch = "\n".join(
        (
            "diff --git a/a.txt b/a.txt",
            "--- a/a.txt",
            "+++ b/a.txt",
            "@@ -1 +1 @@",
            "-old",
            "+new",
        )
    )
    run_id = 7_700_777
    cancellation_registry.clear(run_id)
    context = ToolExecutionContext(
        task_id=1, workspace_id=1, workspace_root=tmp_path, run_id=run_id
    )
    cancellation_registry.mark_cancelled(run_id)
    try:
        observation = ApplyPatchTool().execute(context, patch=patch)
    finally:
        cancellation_registry.clear(run_id)

    assert observation.status == "cancelled"
    assert target.read_bytes() == b"old\n"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
