"""应用 Git 统一 diff 到已有文件时的严格入参。

仅约定字段级契约：被接受的 diff 格式与 hunk 计数规则（按正文重算，不要求模型精确计数）
的唯一事实源是下方 ``patch`` 字段的描述，最小示例随该字段的 ``examples`` 导出。工具描述
（``apply_patch_tool.APPLY_PATCH_DESCRIPTION``）只承载工具职责及其与 ``write_file`` 和终端
（删除、移动）的边界，绝不重复该格式，从而两处文本不会漂移。
"""

from pydantic import BaseModel, ConfigDict, Field


class ApplyPatchArgs(BaseModel):
    """接受一段修改工作区已有文件的 Git 风格统一 diff。

    模型只携带 ``patch`` 字段；该字段的描述即为被接受 diff 格式的唯一事实源。传入值只能修改
    工作区内的已有 UTF-8 文本文件：创建、删除、重命名、复制、二进制与合并 diff 均不在契约内。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    patch: str = Field(
        description=(
            "Raw Git unified diff text only: no prose, no '*** Begin Patch' / '*** End Patch' "
            "wrapper. Each file section needs a 'diff --git' header, a matching '---' / '+++' "
            "pair, and at least one content hunk; every header must use the same normalized "
            "workspace-relative path. Body lines start with ' ' (context), '-' (removed) or "
            "'+' (added); hunk line counts are recomputed from the body. Every section must "
            "modify an existing UTF-8 text file: creation, deletion, moves, copies, mode-only "
            "changes, binaries and merge diffs are rejected, so use write_file or "
            "execute_terminal for those.\n"
        ),
        # 每行必须自带 "\n"：相邻字符串字面量会被直接拼接，漏掉换行时样例会塌成一行，
        # 模型照抄即被解析器拒绝。
        examples=[(
            "diff --git a/pkg/mod.py b/pkg/mod.py\n"
            "--- a/pkg/mod.py\n"
            "+++ b/pkg/mod.py\n"
            "@@ -10,3 +10,3 @@\n"
            " import os\n"
            "-old_line()\n"
            "+new_line()\n"
            " keep()\n"
        )],
    )
