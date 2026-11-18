from app.tools.display.file_change_display import (
    build_file_change_display_data,
)
from app.tools.tool_handler.file_edit.patch_diff import FileDiffResult, format_git_diff


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


# 目的：超大文件的 diff 不进快照与推送，卡片仍能说出改了多少；潜在缺陷：整段下发撑爆快照，或连计数一起丢掉。
def test_patch_over_the_display_budget_is_omitted_but_counts_remain(monkeypatch) -> None:
    from app.config.constant import Constant

    monkeypatch.setattr(Constant.Tools, "DISPLAY_PATCH_MAX_CHARS", 200)
    small = FileDiffResult(path="a.py", status="modified", before="old\n", after="new\n")
    large = FileDiffResult(path="b.py", status="modified", before="old\n" * 80, after="new\n" * 80)

    changes = build_file_change_display_data([small, large])["changes"]

    assert changes[0]["patch"] == format_git_diff(small)
    assert changes[1]["patch"] is None
    assert (changes[1]["insertions"], changes[1]["deletions"]) == (80, 80)
