"""apply_patch 工具（原 patch 工具的 patch 模式）的 Pydantic 参数模型。

字段单一必填，必需性由模型静态校验保证，不再像原 ``PatchArgs`` 那样把所有字段
压进一个模型后在 handler 内按 ``mode`` 运行时校验。

校验边界：``extra="forbid"`` 拒绝任何多余字段，``strict=True`` 拒绝类型错误。
"""

from pydantic import BaseModel, ConfigDict, Field


class ApplyPatchArgs(BaseModel):
    """apply_patch 工具接受的校验参数（V4A 多文件补丁）。

    字段：
        patch: V4A 格式 patch 文本（必填）。

    校验边界：``extra="forbid"`` 拒绝任何多余字段，``strict=True`` 拒绝类型错误。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    patch: str = Field(
        description=(
            "A single V4A-format patch that can modify multiple files at once. Use this "
            "tool instead of 'patch' (single-file replace) when you need to edit several "
            "files in one atomic operation.\n"
            "Format (Hermes-style, required exactly):\n"
            "  *** Begin Patch\n"
            "  *** Update File: path/to/file.py\n"
            "  @@\n"
            "   context line (unchanged)\n"
            "  -removed line\n"
            "  +added line\n"
            "  *** Add File: path/to/new.py\n"
            "  +full new content line 1\n"
            "  +full new content line 2\n"
            "  *** Delete File: path/to/obsolete.py\n"
            "  *** End Patch\n"
            "Rules: wrap everything in '*** Begin Patch' / '*** End Patch'; prefix each "
            "line with '  ' (space) for context, '-' to remove, '+' to add; use '*** "
            "Update File:' / '*** Add File:' / '*** Delete File:' per file. Keep context "
            "lines exact. This field is required."
        )
    )
