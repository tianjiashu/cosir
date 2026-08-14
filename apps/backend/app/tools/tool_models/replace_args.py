"""replace 工具（原 patch 工具的 replace 模式）的 Pydantic 参数模型。

字段全部必填（除 ``replace_all`` 外），必需性由模型静态校验保证，不再像原
``PatchArgs`` 那样把所有字段压进一个模型后在 handler 内按 ``mode`` 运行时校验。

校验边界：``extra="forbid"`` 拒绝任何多余字段，``strict=True`` 拒绝类型错误。
"""

from pydantic import BaseModel, ConfigDict, Field


class ReplaceArgs(BaseModel):
    """replace 工具接受的校验参数（单文件模糊查找替换）。

    字段：
        path: 待编辑文件的相对路径（必填）。
        old_string: 待查找的精确文本（必填）。
        new_string: 替换文本（必填，空串表示删除匹配文本）。
        replace_all: 为 True 替换所有命中，否则要求唯一命中（默认 False）。

    校验边界：``extra="forbid"`` 拒绝任何多余字段，``strict=True`` 拒绝类型错误。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(
        description=(
            "Path to the file to edit, relative to the workspace root "
            "(e.g. 'src/utils/format.py'). Use forward slashes. Must be an existing file."
        )
    )
    old_string: str = Field(
        description=(
            "Exact, unique code/text snippet to locate in the file. Must match verbatim "
            "including whitespace and indentation. Copy it directly from the file; do not "
            "summarize or paraphrase. Required to match exactly one location unless "
            "'replace_all' is true."
        )
    )
    new_string: str = Field(
        description=(
            "The exact text that replaces 'old_string'. Pass an empty string '' to delete "
            "the matched text. If editing only part of a line, keep the unchanged parts "
            "identical to 'old_string' and change only the intended segment."
        )
    )
    replace_all: bool = Field(
        default=False,
        description=(
            "When true, replaces every occurrence of 'old_string'. When false (default), "
            "'old_string' must appear exactly once; the call fails if it is missing or "
            "matched more than once."
        ),
    )
