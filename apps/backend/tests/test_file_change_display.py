"""``build_file_change_display_data`` 展示元数据构造的边界测试。

覆盖目标：``changes[]`` 与 ``diff_stats.files[]`` 必须按输入顺序一一对应，尤其
同一 path 多次变更时，各条 change 必须取到自己那条的统计（不能按 path 建字典，
否则后一条覆盖前一条导致错配）。

本文件只读取 ``apps/backend/app/**`` 业务代码，不修改它们。
"""

from app.tools.tool_handler.patch.file_change_display import build_file_change_display_data
from app.tools.tool_handler.patch.patch_diff import FileDiffResult


def test_single_modified_file_change_matches_stats() -> None:
    """单文件 modified：changes 与 diff_stats 的增删统计一致。"""

    results = [
        FileDiffResult(path="a.py", status="modified", before="x\n", after="x\ny\n"),
    ]
    data = build_file_change_display_data(results)

    assert data["diff_stats"]["total_files"] == 1
    assert data["diff_stats"]["total_insertions"] == 1
    assert data["diff_stats"]["total_deletions"] == 0
    assert len(data["changes"]) == 1
    change = data["changes"][0]
    assert change["path"] == "a.py"
    assert change["status"] == "modified"
    assert change["insertions"] == 1
    assert change["deletions"] == 0


def test_multiple_distinct_paths_align_by_order() -> None:
    """多文件不同 path：每条 change 取到自己文件的统计。"""

    results = [
        FileDiffResult(path="a.py", status="modified", before="x\n", after="x\ny\nz\n"),
        FileDiffResult(path="b.py", status="modified", before="a\nb\n", after="a\n"),
    ]
    data = build_file_change_display_data(results)

    stats_files = data["diff_stats"]["files"]
    assert len(stats_files) == 2
    assert [c["path"] for c in data["changes"]] == ["a.py", "b.py"]
    assert [c["insertions"] for c in data["changes"]] == [2, 0]
    assert [c["deletions"] for c in data["changes"]] == [0, 1]


def test_duplicate_path_stats_are_isolated() -> None:
    """同一 path 多次变更：各条 change 必须取到自己那条的统计（回归）。

    缺陷复现：按 path 建 ``stat_by_path`` 字典时，同 path 第二条会覆盖第一条，
    导致两条 change 都取到最后一条的统计（insertions=[0,0] / deletions=[1,1]）。
    顺序 zip 对齐下应为 insertions=[2,0] / deletions=[0,1]。
    """

    results = [
        FileDiffResult(path="a.py", status="modified", before="x\n", after="x\ny\nz\n"),
        FileDiffResult(path="a.py", status="modified", before="x\ny\nz\n", after="x\ny\n"),
    ]
    data = build_file_change_display_data(results)

    changes = data["changes"]
    assert [c["path"] for c in changes] == ["a.py", "a.py"]
    assert [c["insertions"] for c in changes] == [2, 0]
    assert [c["deletions"] for c in changes] == [0, 1]

    # 聚合统计与 files[] 顺序对齐，不被 path 覆盖影响。
    stats_files = data["diff_stats"]["files"]
    assert len(stats_files) == 2
    assert stats_files[0]["insertions"] == 2
    assert stats_files[1]["deletions"] == 1
    assert data["diff_stats"]["total_insertions"] == 2
    assert data["diff_stats"]["total_deletions"] == 1


def test_moved_file_counts_zero_and_new_path_passthrough() -> None:
    """moved 状态：增删计 0，new_path 透传进 changes 与 files[]。"""

    results = [
        FileDiffResult(
            path="old.py",
            status="moved",
            before="x\n",
            after="x\n",
            new_path="new.py",
        ),
    ]
    data = build_file_change_display_data(results)

    change = data["changes"][0]
    assert change["path"] == "old.py"
    assert change["new_path"] == "new.py"
    assert change["status"] == "moved"
    assert change["insertions"] == 0
    assert change["deletions"] == 0
    assert data["diff_stats"]["files"][0]["new_path"] == "new.py"
