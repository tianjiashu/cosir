from app.tools.display.file_change_display import (
    build_file_change_display_data,
)
from app.tools.tool_handler.patch_write.patch_diff import FileDiffResult, format_git_diff


def test_display_data_uses_git_patch_without_full_snapshots() -> None:
    result = FileDiffResult(
        path="src/app.py",
        status="modified",
        before="old\n",
        after="new\n",
    )

    display = build_file_change_display_data([result])

    change = display["changes"][0]
    assert change["patch"].startswith("diff --git a/src/app.py b/src/app.py\n")
    assert "--- a/src/app.py" in change["patch"]
    assert "+++ b/src/app.py" in change["patch"]
    assert "before" not in change
    assert "after" not in change


def test_git_patch_marks_added_files_from_dev_null() -> None:
    patch = format_git_diff(FileDiffResult(path="new.py", status="added", before="", after="value\n"))

    assert "--- /dev/null" in patch
    assert "+++ b/new.py" in patch


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
