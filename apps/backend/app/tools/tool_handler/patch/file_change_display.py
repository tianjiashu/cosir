"""文件修改工具的 diff 展示投影。"""

from pathlib import Path
from typing import Any

from app.tools.tool_handler.patch.patch_diff import (
    FileDiffResult,
    build_diff_stats,
    format_unified_diff,
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

    stats = build_diff_stats(results)
    stat_by_path = {str(file_stat.get("path")): file_stat for file_stat in stats.get("files", [])}
    changes = [_build_file_change(result, stat_by_path.get(result.path, {})) for result in results]
    return {
        "changes": changes,
        "diff_stats": stats,
    }


def render_file_change_entries(display_data: dict[str, Any]) -> str | list[dict[str, Any]] | None:
    """把文件修改元数据投影为前端 diff 展示条目。

    参数:
        display_data: 工具观察中的展示元数据。

    返回:
        失败时返回错误摘要；无文本变化时返回空状态文案；有变化时返回 diff 条目列表。

    异常:
        无。

    副作用:
        无。
    """

    if display_data.get("status") == "error":
        return "error:" + str(display_data.get("error", ""))
    changes = [change for change in display_data.get("changes", []) if isinstance(change, dict)]
    entries = [
        entry
        for entry in (_render_file_change_entry(change) for change in changes)
        if str(entry.get("diff") or "")
    ]
    if not entries:
        return "（没有文本变更）"
    return entries


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


def _file_change_diff(change: dict[str, Any]) -> str:
    """生成单文件修改的 diff 文本。

    参数:
        change: 单文件修改元数据。

    返回:
        unified diff 文本；移动操作返回移动说明。

    异常:
        无。

    副作用:
        无。
    """

    path = str(change.get("path") or "")
    new_path = change.get("new_path")
    if change.get("status") == "moved":
        return f"# Moved: {path} -> {new_path}"
    before = str(change.get("before") or "")
    after = str(change.get("after") or "")
    diff = format_unified_diff(before, after, path)
    return "" if diff == "(no textual change)" else diff


def _render_file_change_entry(change: dict[str, Any]) -> dict[str, Any]:
    """把单文件修改元数据转换为前端 diff 条目。

    参数:
        change: 单文件修改元数据。

    返回:
        前端必要的 diff 展示字段。

    异常:
        无。

    副作用:
        无。
    """

    path = str(change.get("path") or "")
    name = Path(path).name or path
    diff = _file_change_diff(change)
    return {
        "kind": "file_diff",
        "icon": "git-compare",
        "name": name,
        "path": path,
        "new_path": change.get("new_path"),
        "status": str(change.get("status") or "modified"),
        "diff": diff,
        "insertions": int(change.get("insertions", 0)),
        "deletions": int(change.get("deletions", 0)),
    }
