"""文件修改工具的展示数据构造。

只产出客户端渲染所需的结构化事实数据（变更列表 + 完整 Git patch + 逐文件增删行数），不产出
摘要或展示条目；展示布局由客户端渲染规则层生成。展示数据不经过模型输出预算截断。

职责边界：
- 负责：``file-changes`` 这一个展示 ``kind`` 的全部投影（``write_file`` / ``edit_file`` /
  ``apply_patch`` 的 ``modified`` / ``added``）。删除与移动走终端，没有文件变更卡片。
- 不负责：读取文件内容、回退业务事实或渲染规则。
"""

import re
from typing import Any

from app.config.constant import Constant
from app.tools.tool_handler.file.file_edit.patch_diff import (
    FileDiffResult,
    count_diff_lines,
    format_git_diff,
)

# 补丁里的目标文件：只认 ``diff --git a/<path> b/<path>`` 头；标题不做完整解析，解析与校验归工具。
_PATCH_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/", re.MULTILINE)


def build_file_change_display_data(results: list[FileDiffResult]) -> dict[str, Any]:
    """把文件修改快照转换为展示层事实元数据。

    参数:
        results: 文件修改前后的内容快照。

    返回:
        包含 ``changes`` 的展示元数据（逐文件计数，总数由客户端相加）。每个 change 使用完整的
        Git 风格 ``patch``，不因文本长度设置 ``truncated``。

    异常:
        无。

    副作用:
        无。
    """

    changes = [_build_file_display_change(result) for result in results]
    return {
        "kind": "file-changes",
        "changes": changes,
    }


def _build_file_display_change(result: FileDiffResult) -> dict[str, Any]:
    """构造单文件完整 Diff 展示数据，不携带完整文件快照。"""

    insertions, deletions = count_diff_lines(result)
    patch = format_git_diff(result)
    return {
        "path": result.path,
        "status": result.status,
        # 超出展示上限的 patch 不下发（null），卡片只留增删行数：大文件 diff 不进快照、推送与渲染。
        "patch": patch if len(patch) <= Constant.Tools.DISPLAY_PATCH_MAX_CHARS else None,
        "insertions": insertions,
        "deletions": deletions,
    }
