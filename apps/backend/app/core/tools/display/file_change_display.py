"""文件修改工具的展示数据构造。

只产出客户端渲染所需的结构化事实数据（变更列表 + 统计），不产出任何展示文本或
展示条目；diff 条目与摘要由客户端渲染规则层生成。
"""

from typing import Any

from app.core.tools.tool_handler.patch.patch_diff import (
    FileDiffResult,
    build_diff_stats,
)


def build_file_change_display_data(results: list[FileDiffResult]) -> dict[str, Any]:
    """把文件修改快照转换为展示层事实元数据。

    参数:
        results: 文件修改前后的内容快照。

    返回:
        包含 ``changes`` 与 ``diff_stats`` 的展示元数据。

    异常:
        无。

    副作用:
        无。
    """

    # 顺序对齐：build_diff_stats 的 files[] 与输入一一对应（不按 path 去重），
    # 故直接按序配对。不能按 path 建字典——同一 path 多次变更时后一条会覆盖
    # 前一条，导致多条 change 共享最后一条统计。
    stats = build_diff_stats(results)
    file_stats = stats.get("files", [])
    changes = [
        _build_file_change(result, file_stat)
        for result, file_stat in zip(results, file_stats, strict=True)
    ]
    return {
        "kind": "file-changes",
        "changes": changes,
        "diff_stats": stats,
    }


def _build_file_change(result: FileDiffResult, file_stat: dict[str, Any]) -> dict[str, Any]:
    """构造单文件修改展示元数据。

    参数:
        result: 单文件修改快照。
        file_stat: ``build_diff_stats`` 产出的单文件统计。

    返回:
        包含路径、状态、diff 和增删行数的字典。

    异常:
        无。

    副作用:
        无。
    """

    return {
        "path": result.path,
        "new_path": result.new_path,
        "status": result.status,
        "before": result.before,
        "after": result.after,
        "insertions": int(file_stat.get("insertions", 0)),
        "deletions": int(file_stat.get("deletions", 0)),
    }
