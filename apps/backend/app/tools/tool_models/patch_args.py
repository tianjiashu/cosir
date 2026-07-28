"""patch 工具的 Pydantic 参数模型。

replace 与 patch 两种模式共用同一个模型：``mode='replace'`` 需要
``path`` / ``old_string`` / ``new_string``，``mode='patch'`` 需要 ``patch``（V4A）。
字段全可选，必需性在 handler 内按 ``mode`` 运行时校验，契合「静态校验层只验类型、
运行时校验必需项」的分工。

校验边界：``extra="forbid"`` 拒绝任何多余字段，``strict=True`` 拒绝类型错误。
"""

from pydantic import BaseModel, ConfigDict, Field


class PatchArgs(BaseModel):
    """patch 工具接受的校验参数（replace 与 patch 两种模式共用）。

    字段：
        mode: 编辑模式，``replace``（默认）或 ``patch``。
        path: mode='replace' 时必填，待编辑文件的相对路径。
        old_string: mode='replace' 时必填，待查找的文本。
        new_string: mode='replace' 时必填，替换文本（空串表示删除匹配文本）。
        replace_all: 为 True 替换所有命中，否则要求唯一命中（默认 False）。
        patch: mode='patch' 时必填，V4A 格式 patch 文本。

    校验边界：``extra="forbid"`` 拒绝任何多余字段，``strict=True`` 拒绝类型错误。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    mode: str = Field(
        default="replace",
        description=(
            "Edit mode: 'replace' (default) requires path + old_string + new_string; "
            "'patch' requires patch content (V4A)."
        ),
    )
    path: str | None = Field(
        default=None, description="REQUIRED when mode='replace'. File path to edit."
    )
    old_string: str | None = Field(
        default=None,
        description="REQUIRED when mode='replace'. Exact text to find and replace.",
    )
    new_string: str | None = Field(
        default=None,
        description=(
            "REQUIRED when mode='replace'. Replacement text. Pass empty string '' to "
            "delete the matched text."
        ),
    )
    replace_all: bool = Field(
        default=False,
        description="Replace all occurrences instead of requiring a unique match (default: false).",
    )
    patch: str | None = Field(
        default=None,
        description="REQUIRED when mode='patch'. V4A format patch content.",
    )
