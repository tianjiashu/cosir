"""文件修改工具的展示数据构造。

只产出客户端渲染所需的结构化事实数据（变更列表 + 完整 Git patch + 统计），不产出
摘要或展示条目；展示布局由客户端渲染规则层生成。展示数据不经过模型输出预算截断。

职责边界：
- 负责：``file-changes`` 这一个展示 ``kind`` 的全部投影——内容修改类（``write_file`` /
  ``patch_write`` / ``apply_patch`` 的 ``modified`` / ``added``）与删除类
  （``delete_file`` 的「目标 + 状态」摘要）。
- 不负责：读取文件内容（内容由各 handler 或 ``FileMutationService`` 的 before-image 提供）、
  回退事实（由 ``FileMutationService`` 持久化）、渲染规则。
"""

from typing import Any

from app.core.tools.tool_handler.patch_write.patch_diff import (
    FileDiffResult,
    build_diff_stats,
    format_git_diff,
)


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


def build_file_delete_display_data(path: str) -> dict[str, Any]:
    """构造 ``delete_file`` 的「目标 + 状态」展示数据（不携带被删内容）。

    删除不需要内容就能表达完整事实：客户端只需知道哪个路径被删除。被删文件的正文属于
    回退事实，由 ``FileMutationService`` 的 before-image 承载，不进入 UI 展示通道，
    也不从模型通道回显，因此本函数不读取文件、不生成 Diff。

    参数:
        path: 被删除文件的工作区相对路径（POSIX 分隔符）。

    返回:
        ``kind="file-changes"`` 的展示元数据：一条 ``deleted`` 变更（``patch`` 为
        ``None``、增删统计为 0）与零值 ``diff_stats``；形状与
        :func:`build_file_change_display_data` 保持一致，客户端沿用同一渲染分支。

    异常:
        无。

    副作用:
        无（只构造字典，不访问文件系统）。
    """

    return {
        "kind": "file-changes",
        "changes": [
            {
                "path": path,
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
                    "path": path,
                    "status": "deleted",
                    "insertions": 0,
                    "deletions": 0,
                    "new_path": None,
                }
            ],
        },
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
