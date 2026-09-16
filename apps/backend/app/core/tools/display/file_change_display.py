"""文件修改工具的展示数据构造。

只产出客户端渲染所需的结构化事实数据（变更列表 + 完整 Git patch + 统计），不产出
摘要或展示条目；展示布局由客户端渲染规则层生成。展示数据不经过模型输出预算截断。
"""

from typing import Any

from app.core.tools.tool_handler.patch_write.patch_diff import (
    FileDiffResult,
    build_diff_stats,
    format_git_diff,
)


def build_file_change_artifact_data(results: list[FileDiffResult]) -> dict[str, Any]:
    """把文件修改快照投影为内部回退与审计工件数据。

    参数:
        results: 文件修改前后的完整内容快照。

    返回:
        包含完整 ``before``/``after``、路径、状态和统计的内部数据。

    异常:
        无。

    副作用:
        无；不读写文件或数据库。

    说明:
        该数据只供后端 Hook、ChangeSet 和回退流程使用，不进入 Assistant Transport。
    """

    stats = build_diff_stats(results)
    file_stats = stats.get("files", [])
    changes = [
        _build_file_artifact_change(result, file_stat)
        for result, file_stat in zip(results, file_stats, strict=True)
    ]
    return {"changes": changes, "diff_stats": stats}


def build_file_change_display_data(results: list[FileDiffResult]) -> dict[str, Any]:
    """把文件修改快照转换为展示层事实元数据。

    参数:
        results: 文件修改前后的内容快照。

    返回:
        包含 ``changes`` 与 ``diff_stats`` 的展示元数据。每个 change 使用完整的
        Git 风格 ``patch``，不因文本长度设置 ``truncated``。

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
        _build_file_display_change(result, file_stat)
        for result, file_stat in zip(results, file_stats, strict=True)
    ]
    return {
        "kind": "file-changes",
        "changes": changes,
        "diff_stats": stats,
    }


def _build_file_artifact_change(
    result: FileDiffResult,
    file_stat: dict[str, Any],
) -> dict[str, Any]:
    """构造单文件完整快照工件。"""

    return {
        "path": result.path,
        "new_path": result.new_path,
        "status": result.status,
        "before": result.before,
        "after": result.after,
        "insertions": int(file_stat.get("insertions", 0)),
        "deletions": int(file_stat.get("deletions", 0)),
    }


def _build_file_display_change(
    result: FileDiffResult,
    file_stat: dict[str, Any],
) -> dict[str, Any]:
    """构造单文件完整 Diff 展示数据，不携带完整文件快照。"""

    patch = format_git_diff(result)
    return {
        "path": result.path,
        "new_path": result.new_path,
        "status": result.status,
        "patch": patch,
        "insertions": int(file_stat.get("insertions", 0)),
        "deletions": int(file_stat.get("deletions", 0)),
    }
