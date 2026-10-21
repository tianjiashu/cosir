"""应用 Git 统一 diff 到已有文件时的严格入参。

仅约定字段级契约：被接受的 diff 格式与 hunk 计数规则（按正文重算，不要求模型精确计数）
的唯一事实源是下方 ``patch`` 字段的描述，最小示例随该字段的 ``examples`` 导出。工具描述
（``apply_patch_tool.APPLY_PATCH_DESCRIPTION``）只承载工具职责及其与 ``write_file`` /
``delete_file`` / ``move_file`` 的边界，绝不重复该格式，从而两处文本不会漂移。
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
            "只传入原始 Git 统一 diff 文本：不要夹杂说明文字，也不要用 '*** Begin Patch' / "
            "'*** End Patch' 包裹。每个文件段需要 'diff --git' 头、一对匹配的 '---' / '+++' "
            "路径，以及至少一个内容差异块；所有头部必须使用同一个归一化的工作区相对路径。"
            "差异块正文行以 ' '（上下文）、'-'（删除）或 '+'（新增）开头；差异块行数按正文"
            "重算。每个文件段必须修改一个已有的 UTF-8 文本文件。创建、删除、移动、复制、"
            "仅改模式、二进制以及合并 diff 均不支持，这些操作请改用 write_file、delete_file "
            "或 move_file。\n"
        ),
        examples=[(
            "diff --git a/pkg/mod.py b/pkg/mod.py"
            "--- a/pkg/mod.py"
            "+++ b/pkg/mod.py"
            "@@ -10,3 +10,3 @@"
            " import os"
            "-old_line()"
            "+new_line()"
            " keep()"
        )]
    )
