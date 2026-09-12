from pathlib import Path
from types import SimpleNamespace

from app.core.tools.schemas import ToolExecutionContext
from app.core.tools.tool_handler.apply_patch_tool import ApplyPatchTool
from app.core.tools.tool_handler.patch.patch_apply import PatchApplyError
from app.core.tools.tool_handler.patch.patch_diff import FileDiffResult


def _context(tmp_path: Path) -> ToolExecutionContext:
    return ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=tmp_path)


def test_success_returns_success_without_repeating_the_patch_diff(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / "a.py").write_text("new\n", encoding="utf-8")
    result = FileDiffResult(path="a.py", status="modified", before="old\n", after="new\n")
    monkeypatch.setattr(
        "app.core.tools.tool_handler.apply_patch_tool.parse_v4a_patch",
        lambda _patch: ([object()], None),
    )
    monkeypatch.setattr(
        "app.core.tools.tool_handler.apply_patch_tool.validate_all",
        lambda _operations, _resolver: [],
    )
    monkeypatch.setattr(
        "app.core.tools.tool_handler.apply_patch_tool.apply_all_with_diff",
        lambda _operations, _resolver: [result],
    )
    monkeypatch.setattr(
        "app.core.tools.tool_handler.apply_patch_tool.check_source_syntax",
        lambda _path, _content: SimpleNamespace(has_error=False),
    )
    monkeypatch.setattr(
        "app.core.tools.tool_handler.apply_patch_tool.build_file_change_display_data",
        lambda _results: {"kind": "file-changes"},
    )
    monkeypatch.setattr(
        "app.core.tools.tool_handler.apply_patch_tool.build_file_change_artifact_data",
        lambda _results: {"changes": [{"path": "a.py"}]},
    )
    observation = ApplyPatchTool().execute(
        _context(tmp_path), patch="*** Begin Patch\n*** End Patch"
    )

    assert observation.status == "success"
    assert observation.content is None
    assert observation.display_data == {"kind": "file-changes"}
    assert observation.artifact_data == {"changes": [{"path": "a.py"}]}


def test_empty_patch_can_be_corrected_and_retried_separately_from_its_reason(
    tmp_path: Path,
) -> None:
    observation = ApplyPatchTool().execute(_context(tmp_path), patch="")

    assert observation.retryable is True
    assert observation.error == "missing patch input"
    assert observation.reason == "provide a non-empty V4A patch in the 'patch' argument."


def test_transient_apply_failure_is_retryable_without_duplicate_diagnostics(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "app.core.tools.tool_handler.apply_patch_tool.parse_v4a_patch",
        lambda _patch: ([object()], None),
    )
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

    observation = ApplyPatchTool().execute(
        _context(tmp_path), patch="*** Begin Patch\n*** End Patch"
    )

    assert observation.retryable is True
    assert observation.error == "patch apply failed: file is locked"
    assert observation.reason == (
        "resolve the reported condition or regenerate the patch from current file contents "
        "before retrying."
    )
    assert "file is locked" not in observation.reason


def test_partial_apply_failure_is_non_retryable_even_when_cause_is_transient(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "app.core.tools.tool_handler.apply_patch_tool.parse_v4a_patch",
        lambda _patch: ([object()], None),
    )
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

    observation = ApplyPatchTool().execute(
        _context(tmp_path), patch="*** Begin Patch\n*** End Patch"
    )

    assert observation.retryable is False
    assert observation.error == "patch application stopped after partial changes"
    assert observation.reason == (
        "inspect the changed files and create a new patch from the current contents; "
        "do not replay this patch unchanged."
    )
    assert "file is locked" not in observation.reason


def test_patch_content_race_is_retryable_after_regenerating_the_patch(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "app.core.tools.tool_handler.apply_patch_tool.parse_v4a_patch",
        lambda _patch: ([object()], None),
    )
    monkeypatch.setattr(
        "app.core.tools.tool_handler.apply_patch_tool.validate_all",
        lambda _operations, _resolver: [],
    )

    def fail_apply(_operations, _resolver):
        try:
            raise RuntimeError("hunk no longer matches")
        except RuntimeError as cause:
            raise PatchApplyError(
                "hunk no longer matches", partial_applied=False
            ) from cause

    monkeypatch.setattr(
        "app.core.tools.tool_handler.apply_patch_tool.apply_all_with_diff", fail_apply
    )

    observation = ApplyPatchTool().execute(
        _context(tmp_path), patch="*** Begin Patch\n*** End Patch"
    )

    assert observation.retryable is True
    assert observation.error == "patch apply failed: hunk no longer matches"
    assert observation.reason == (
        "resolve the reported condition or regenerate the patch from current file contents "
        "before retrying."
    )
    assert "hunk no longer matches" not in observation.reason
