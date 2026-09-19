from app.core.tools.display.file_change_display import (
    build_file_change_display_data,
    build_file_delete_display_data,
)
from app.core.tools.tool_handler.patch_write.patch_diff import FileDiffResult, format_git_diff


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


def test_delete_display_data_carries_only_the_target_and_status() -> None:
    display = build_file_delete_display_data("logs/old.log")

    assert display == {
        "kind": "file-changes",
        "changes": [
            {
                "path": "logs/old.log",
                "new_path": None,
                "status": "deleted",
                "patch": None,
                "insertions": 0,
                "deletions": 0,
            }
        ],
        "diff_stats": {
            "total_files": 1,
            "total_insertions": 0,
            "total_deletions": 0,
            "files": [
                {
                    "path": "logs/old.log",
                    "status": "deleted",
                    "insertions": 0,
                    "deletions": 0,
                    "new_path": None,
                }
            ],
        },
    }
    # 被删内容不进入展示通道：载荷里不存在任何正文或 Diff 字段。
    assert "old.log" in display["changes"][0]["path"]
    assert "patch" in display["changes"][0]
    assert display["changes"][0]["patch"] is None


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
