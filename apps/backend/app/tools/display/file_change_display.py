"""文件修改工具的展示数据构造。

只产出客户端渲染所需的结构化事实数据（变更列表 + 完整 Git patch + 统计），不产出
摘要或展示条目；展示布局由客户端渲染规则层生成。展示数据不经过模型输出预算截断。

职责边界：
- 负责：``file-changes`` 这一个展示 ``kind`` 的全部投影（``write_file`` / ``patch_write`` /
  ``apply_patch`` 的 ``modified`` / ``added``）。删除与移动走终端，没有文件变更卡片。
- 不负责：读取文件内容、回退业务事实或渲染规则。
"""

import re
from typing import Any

from app.tools.tool_handler.patch_write.patch_diff import (
    FileDiffResult,
    build_diff_stats,
    format_git_diff,
)

# 补丁里的目标文件：只认 ``diff --git a/<path> b/<path>`` 头；标题不做完整解析，解析与校验归工具。
_PATCH_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/", re.MULTILINE)


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


def _build_file_display_change(
    result: FileDiffResult,
    file_stat: dict[str, Any],
) -> dict[str, Any]:
    """构造单文件完整 Diff 展示数据，不携带完整文件快照。"""

    patch = format_git_diff(result)
    return {
        "path": result.path,
        "status": result.status,
        "patch": patch,
        "insertions": int(file_stat.get("insertions", 0)),
        "deletions": int(file_stat.get("deletions", 0)),
    }
