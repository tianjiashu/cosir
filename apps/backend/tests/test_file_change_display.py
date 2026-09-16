from app.core.tools.display.file_change_display import (
    build_file_change_artifact_data,
    build_file_change_display_data,
)
from app.core.tools.tool_handler.patch_write.patch_diff import FileDiffResult, format_git_diff


def test_display_data_uses_git_patch_and_artifact_keeps_full_snapshots() -> None:
    result = FileDiffResult(
        path="src/app.py",
        status="modified",
        before="old\n",
        after="new\n",
    )

    display = build_file_change_display_data([result])
    artifact = build_file_change_artifact_data([result])

    change = display["changes"][0]
    assert change["patch"].startswith("diff --git a/src/app.py b/src/app.py\n")
    assert "--- a/src/app.py" in change["patch"]
    assert "+++ b/src/app.py" in change["patch"]
    assert "before" not in change
    assert "after" not in change
    assert artifact["changes"][0]["before"] == result.before
    assert artifact["changes"][0]["after"] == result.after


def test_git_patch_handles_added_deleted_and_moved_files() -> None:
    assert "--- /dev/null" in format_git_diff(
        FileDiffResult(path="new.py", status="added", before="", after="value\n")
    )
    assert "+++ /dev/null" in format_git_diff(
        FileDiffResult(path="old.py", status="deleted", before="value\n", after="")
    )
    moved = format_git_diff(
        FileDiffResult(
            path="old.py",
            new_path="new.py",
            status="moved",
            before="value\n",
            after="value\n",
        )
    )
    assert "similarity index 100%" in moved
    assert "rename from old.py" in moved
    assert "rename to new.py" in moved


def test_display_data_keeps_the_complete_oversized_patch() -> None:
    result = FileDiffResult(
        path="large.py",
        status="modified",
        before="old\n" * 600,
        after="new\n" * 600,
    )

    change = build_file_change_display_data([result])["changes"][0]

    assert change["patch"] == format_git_diff(result)
    assert "truncated" not in change
