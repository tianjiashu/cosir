import os
from pathlib import Path

import pytest

from app.core.tools.guard.file_resource_paths import FileResourceResolver
from app.core.tools.schemas import ToolExecutionContext
from app.core.tools.tool_handler.apply_patch_tool import APPLY_PATCH_DESCRIPTION, ApplyPatchTool
from app.core.tools.tool_handler.delete_tool import DELETE_FILE_DESCRIPTION, DeleteTool
from app.core.tools.tool_handler.move_tool import MOVE_FILE_DESCRIPTION, MoveTool
from app.core.tools.tool_handler.patch_write.patch_parser import parse_git_unified_diff
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.apply_patch_args import ApplyPatchArgs
from app.core.tools.tool_models.delete_file_args import DeleteFileArgs
from app.core.tools.tool_models.move_file_args import MoveFileArgs
from app.core.tools.tool_system import ToolSystem


def _context(root: Path) -> ToolExecutionContext:
    return ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=root, run_id=991)


def _patch(path: str = "existing.txt") -> str:
    return "\n".join(
        (
            f"diff --git a/{path} b/{path}",
            f"--- a/{path}",
            f"+++ b/{path}",
            "@@ -1 +1 @@",
            "-before",
            "+after",
        )
    )


def test_file_tool_descriptions_and_arguments_express_separate_responsibilities() -> None:
    patch_description = ApplyPatchArgs.model_fields["patch"].description or ""
    delete_description = DeleteFileArgs.model_fields["path"].description or ""
    move_source_description = MoveFileArgs.model_fields["source_path"].description or ""
    move_destination_description = MoveFileArgs.model_fields["destination_path"].description or ""

    assert "existing UTF-8 text files" in APPLY_PATCH_DESCRIPTION
    assert "cannot create, delete, or move" in APPLY_PATCH_DESCRIPTION
    assert "unified diff" in patch_description
    assert "delete_file" in patch_description and "move_file" in patch_description
    assert "Directories are not supported" in DELETE_FILE_DESCRIPTION
    assert "Directories" in delete_description
    assert "directories" in move_source_description.lower()
    assert "must not already exist" in MOVE_FILE_DESCRIPTION
    assert "parent directory must already exist" in MOVE_FILE_DESCRIPTION
    assert "parent directory" in move_destination_description


def test_handlers_inherit_base_and_new_tools_are_registered() -> None:
    assert all(
        issubclass(handler, HandlerBase) for handler in (ApplyPatchTool, DeleteTool, MoveTool)
    )
    names = set(ToolSystem.build_tool_system().registry.get_all_tool_names())

    assert {"apply_patch", "delete_file", "move_file", "write_file"} <= names
    assert "delete" not in names


def test_apply_patch_modifies_existing_file_and_emits_file_changes(tmp_path: Path) -> None:
    target = tmp_path / "existing.txt"
    target.write_text("before\n", encoding="utf-8")

    observation = ApplyPatchTool().execute(_context(tmp_path), patch=_patch())

    assert observation.status == "success"
    assert target.read_text(encoding="utf-8") == "after\n"
    assert observation.display_data["kind"] == "file-changes"
    change = observation.display_data["changes"][0]
    assert change["status"] == "modified"
    assert change["path"] == "existing.txt"


def test_apply_patch_rejects_creation_deletion_move_and_workspace_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("before\n", encoding="utf-8")
    target = tmp_path / "existing.txt"
    target.write_text("before\n", encoding="utf-8")
    patches = (
        "\n".join(
            (
                "diff --git a/new.txt b/new.txt",
                "new file mode 100644",
                "--- /dev/null",
                "+++ b/new.txt",
                "@@ -0,0 +1 @@",
                "+new",
            )
        ),
        "\n".join(
            (
                "diff --git a/existing.txt b/existing.txt",
                "deleted file mode 100644",
                "--- a/existing.txt",
                "+++ /dev/null",
                "@@ -1 +0,0 @@",
                "-before",
            )
        ),
        "\n".join(
            (
                "diff --git a/existing.txt b/renamed.txt",
                "similarity index 100%",
                "rename from existing.txt",
                "rename to renamed.txt",
            )
        ),
        _patch("../outside.txt"),
    )

    for patch in patches:
        operations, error = parse_git_unified_diff(patch)
        assert operations == []
        assert error

    assert target.read_text(encoding="utf-8") == "before\n"
    assert outside.read_text(encoding="utf-8") == "before\n"
    assert not (tmp_path / "new.txt").exists()


def test_apply_patch_supports_git_quoted_paths_with_spaces(tmp_path: Path) -> None:
    target = tmp_path / "existing file.txt"
    target.write_text("before\n", encoding="utf-8")
    patch = "\n".join(
        (
            'diff --git "a/existing file.txt" "b/existing file.txt"',
            '--- "a/existing file.txt"',
            '+++ "b/existing file.txt"',
            "@@ -1 +1 @@",
            "-before",
            "+after",
        )
    )

    observation = ApplyPatchTool().execute(_context(tmp_path), patch=patch)

    assert observation.status == "success"
    assert target.read_text(encoding="utf-8") == "after\n"


def test_apply_patch_rejects_legacy_patch_markers_and_prevalidates_all_files(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("before first\n", encoding="utf-8")
    second.write_text("before second\n", encoding="utf-8")
    legacy_marker = _patch() + "\n*** End Patch"
    multi_file_patch = "\n".join(
        (
            "diff --git a/first.txt b/first.txt",
            "--- a/first.txt",
            "+++ b/first.txt",
            "@@ -1 +1 @@",
            "-before first",
            "+after first",
            "diff --git a/second.txt b/second.txt",
            "--- a/second.txt",
            "+++ b/second.txt",
            "@@ -1 +1 @@",
            "-not the current content",
            "+after second",
        )
    )

    marker_operations, marker_error = parse_git_unified_diff(legacy_marker)
    rejected = ApplyPatchTool().execute(_context(tmp_path), patch=multi_file_patch)

    assert marker_operations == []
    assert marker_error
    assert rejected.status == "error"
    assert first.read_text(encoding="utf-8") == "before first\n"
    assert second.read_text(encoding="utf-8") == "before second\n"


def test_delete_and_move_resolve_and_lock_all_workspace_paths(tmp_path: Path) -> None:
    resources = FileResourceResolver(tmp_path)
    delete = resources.resolve("delete_file", {"path": "src/old.txt"})
    move = resources.resolve(
        "move_file",
        {"source_path": "src/old.txt", "destination_path": "dest/new.txt"},
    )

    assert delete.write_paths == (tmp_path / "src/old.txt",)
    assert move.write_paths == (tmp_path / "src/old.txt", tmp_path / "dest/new.txt")
    assert tmp_path / "src" in delete.lock_paths
    assert tmp_path / "src" in move.lock_paths
    assert tmp_path / "dest" in move.lock_paths


def test_delete_tool_unlinks_one_text_file_but_refuses_a_directory(tmp_path: Path) -> None:
    target = tmp_path / "old.txt"
    target.write_text("removed\n", encoding="utf-8")
    folder = tmp_path / "folder"
    folder.mkdir()

    deleted = DeleteTool().execute(_context(tmp_path), path="old.txt")
    rejected = DeleteTool().execute(_context(tmp_path), path="folder")

    assert deleted.status == "success"
    assert deleted.display_data["changes"][0]["status"] == "deleted"
    assert not target.exists()
    assert rejected.status == "error"
    assert folder.is_dir()


def test_move_tool_moves_one_text_file_without_overwriting_or_creating_parents(
    tmp_path: Path,
) -> None:
    source = tmp_path / "old.txt"
    source.write_text("kept\n", encoding="utf-8")
    destination = tmp_path / "new.txt"
    destination.write_text("existing\n", encoding="utf-8")

    rejected_existing = MoveTool().execute(
        _context(tmp_path), source_path="old.txt", destination_path="new.txt"
    )
    rejected_parent = MoveTool().execute(
        _context(tmp_path), source_path="old.txt", destination_path="missing/new.txt"
    )
    moved = MoveTool().execute(
        _context(tmp_path), source_path="old.txt", destination_path="renamed.txt"
    )

    assert rejected_existing.status == "error"
    assert rejected_parent.status == "error"
    assert destination.read_text(encoding="utf-8") == "existing\n"
    assert moved.status == "success"
    assert moved.display_data["changes"][0]["status"] == "moved"
    assert moved.display_data["changes"][0]["new_path"] == "renamed.txt"
    assert not source.exists()
    assert (tmp_path / "renamed.txt").read_text(encoding="utf-8") == "kept\n"


def test_move_tool_does_not_overwrite_destination_created_during_move(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "old.txt"
    source.write_text("kept\n", encoding="utf-8")
    destination = tmp_path / "new.txt"
    original_move = os.rename if os.name == "nt" else os.link

    def create_destination_then_move(source_path, destination_path) -> None:
        destination.write_text("concurrent\n", encoding="utf-8")
        original_move(source_path, destination_path)

    monkeypatch.setattr(os, "rename" if os.name == "nt" else "link", create_destination_then_move)

    rejected = MoveTool().execute(
        _context(tmp_path), source_path="old.txt", destination_path="new.txt"
    )

    assert rejected.status == "error"
    assert source.read_text(encoding="utf-8") == "kept\n"
    assert destination.read_text(encoding="utf-8") == "concurrent\n"


def test_delete_and_move_reject_final_symbolic_link_paths(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("kept\n", encoding="utf-8")
    alias = tmp_path / "alias.txt"
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("the current platform does not permit creating file symbolic links")

    deleted = DeleteTool().execute(_context(tmp_path), path="alias.txt")
    moved = MoveTool().execute(
        _context(tmp_path), source_path="alias.txt", destination_path="moved.txt"
    )

    assert deleted.status == "error"
    assert moved.status == "error"
    assert alias.is_symlink()
    assert target.read_text(encoding="utf-8") == "kept\n"
    assert not (tmp_path / "moved.txt").exists()
