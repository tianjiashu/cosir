from pathlib import Path
from types import SimpleNamespace

from app.core.tools.schemas import ToolExecutionContext
from app.core.tools.tool_handler.apply_patch_tool import ApplyPatchTool
from app.core.tools.tool_handler.patch_write.patch_apply import PatchApplyError


def _context(tmp_path: Path) -> ToolExecutionContext:
    return ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=tmp_path, run_id=917)


def _valid_patch() -> str:
    return "\n".join(
        (
            "diff --git a/a.txt b/a.txt",
            "--- a/a.txt",
            "+++ b/a.txt",
            "@@ -1 +1 @@",
            "-old",
            "+new",
        )
    )


def test_invalid_diff_is_retryable_with_tool_specific_guidance(tmp_path: Path) -> None:
    observation = ApplyPatchTool().execute(_context(tmp_path), patch="not a diff")

    assert observation.status == "error"
    assert observation.retryable is True
    assert "Git unified diff" in observation.error
    assert "write_file" in observation.reason
    assert "delete_file" in observation.reason
    assert "move_file" in observation.reason


def test_success_returns_success_without_repeating_the_patch(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("new\n", encoding="utf-8")
    monkeypatch.setattr(
        "app.core.tools.tool_handler.apply_patch_tool.validate_all",
        lambda _operations, _resolver: [],
    )
    monkeypatch.setattr(
        "app.core.tools.tool_handler.apply_patch_tool.apply_all_with_diff",
        lambda _operations, _resolver: [],
    )
    monkeypatch.setattr(
        "app.core.tools.tool_handler.apply_patch_tool.check_source_syntax",
        lambda _path, _content: SimpleNamespace(has_error=False),
    )
    monkeypatch.setattr(
        "app.core.tools.tool_handler.apply_patch_tool.build_file_change_display_data",
        lambda _results: {"kind": "file-changes"},
    )
    observation = ApplyPatchTool().execute(_context(tmp_path), patch=_valid_patch())

    assert observation.status == "success"
    assert observation.content is None
    assert observation.display_data == {"kind": "file-changes"}


def test_transient_apply_failure_is_retryable_after_regenerating_the_diff(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "app.core.tools.tool_handler.apply_patch_tool.validate_all",
        lambda _operations, _resolver: [],
    )

    def fail_apply(_operations, _resolver):
        try:
            raise OSError(0, "file is locked", None, 32)
        except OSError as cause:
            raise PatchApplyError("file is locked", partial_applied=False) from cause

    monkeypatch.setattr(
        "app.core.tools.tool_handler.apply_patch_tool.apply_all_with_diff", fail_apply
    )
    observation = ApplyPatchTool().execute(_context(tmp_path), patch=_valid_patch())

    assert observation.retryable is True
    assert observation.error == "unified diff apply failed: file is locked"
    assert "new unified diff" in observation.reason
    assert "file is locked" not in observation.reason


def test_partial_apply_failure_is_not_retryable(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "app.core.tools.tool_handler.apply_patch_tool.validate_all",
        lambda _operations, _resolver: [],
    )

    def fail_after_partial_apply(_operations, _resolver):
        try:
            raise OSError(0, "file is locked", None, 32)
        except OSError as cause:
            raise PatchApplyError("file is locked", partial_applied=True) from cause

    monkeypatch.setattr(
        "app.core.tools.tool_handler.apply_patch_tool.apply_all_with_diff",
        fail_after_partial_apply,
    )
    observation = ApplyPatchTool().execute(_context(tmp_path), patch=_valid_patch())

    assert observation.retryable is False
    assert observation.error == "unified diff application stopped after partial changes"
    assert "do not replay this diff unchanged" in observation.reason
    assert "file is locked" not in observation.reason


def test_hunk_validation_failure_does_not_write_any_file(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("current\n", encoding="utf-8")
    observation = ApplyPatchTool().execute(_context(tmp_path), patch=_valid_patch())

    assert observation.status == "error"
    assert observation.retryable is True
    assert "no files were modified" in observation.error
    assert target.read_text(encoding="utf-8") == "current\n"
