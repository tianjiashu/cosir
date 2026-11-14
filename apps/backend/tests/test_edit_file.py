"""edit_file 与 apply_patch 只认原文：不容忍空白、缩进与 Unicode 差异，失败时给出最近的几行。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.tool.schemas import ToolExecutionContext
from app.tools.tool_handler.apply_patch_tool import ApplyPatchTool
from app.tools.tool_handler.edit_file import EditFileTool
from app.tools.tool_handler.file_edit.text_match import TextMatchError, replace_exact


def _ctx(root: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id=1, workspace_id=1, workspace_root=root, step_id="s", run_id=1, tool_call_id="c"
    )


def _edit(root: Path, **kwargs):
    return EditFileTool().execute(execution_context=_ctx(root), **kwargs)


def test_replace_exact_requires_a_unique_verbatim_match() -> None:
    assert replace_exact("a = 1\nb = 2\n", "b = 2", "b = 3") == ("a = 1\nb = 3\n", 1)
    assert replace_exact("x\nx\n", "x", "y", replace_all=True) == ("y\ny\n", 2)
    with pytest.raises(TextMatchError, match="matches 2 places"):
        replace_exact("x\nx\n", "x", "y")
    with pytest.raises(TextMatchError, match="Did you mean"):
        replace_exact("    value = compute()\n", "value = compute(  )", "v")


@pytest.mark.parametrize(
    "drifted",
    [
        "\treturn 1",  # 缩进不同（制表符）
        "return  1",  # 空白不同
        "return “1”",  # 智能引号
    ],
)
def test_edit_file_refuses_near_matches_and_leaves_the_file_alone(tmp_path: Path, drifted: str) -> None:
    target = tmp_path / "m.py"
    target.write_text('def f():\n    return 1\n    return "1"\n', encoding="utf-8")

    observation = _edit(tmp_path, path="m.py", old_string=drifted, new_string="    return 2")

    assert observation.status == "error"
    assert "no exact match" in (observation.content or "")
    assert target.read_text(encoding="utf-8") == 'def f():\n    return 1\n    return "1"\n'


def test_edit_file_replaces_and_keeps_the_files_line_endings(tmp_path: Path) -> None:
    target = tmp_path / "m.txt"
    target.write_bytes(b"one\r\ntwo\r\nthree\r\n")

    observation = _edit(tmp_path, path="m.txt", old_string="one\ntwo", new_string="one\nTWO")

    assert observation.status == "success", observation.content
    assert target.read_bytes() == b"one\r\nTWO\r\nthree\r\n"
    assert observation.display_data["kind"] == "file-changes"


def test_edit_file_needs_a_unique_match_or_replace_all(tmp_path: Path) -> None:
    target = tmp_path / "m.txt"
    target.write_text("x\nx\n", encoding="utf-8")

    ambiguous = _edit(tmp_path, path="m.txt", old_string="x", new_string="y")
    everywhere = _edit(tmp_path, path="m.txt", old_string="x", new_string="y", replace_all=True)

    assert ambiguous.status == "error" and "matches 2 places" in (ambiguous.content or "")
    assert everywhere.status == "success" and target.read_text(encoding="utf-8") == "y\ny\n"


def test_apply_patch_hunk_with_drifted_context_is_not_applied(tmp_path: Path) -> None:
    target = tmp_path / "m.txt"
    target.write_text("alpha\n    beta\n", encoding="utf-8")
    patch = (
        "diff --git a/m.txt b/m.txt\n--- a/m.txt\n+++ b/m.txt\n"
        "@@ -1,2 +1,2 @@\n alpha\n-beta\n+gamma\n"
    )

    observation = ApplyPatchTool().execute(_ctx(tmp_path), patch=patch)

    assert observation.status == "error"
    assert "hunk 1 not found" in (observation.content or "")
    assert target.read_text(encoding="utf-8") == "alpha\n    beta\n"
