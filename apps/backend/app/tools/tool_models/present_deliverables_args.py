"""present_deliverables 参数校验模型；字段 description 会进入模型可见的工具 schema。"""

from pydantic import BaseModel, ConfigDict, Field

# 一次最多交付几项：卡片是「成果」的清单，多了就退化成文件操作记录。
PRESENT_DELIVERABLES_MAX = 5


class DeliverableTarget(BaseModel):
    """一项交付物：一个字段而不是「路径或网址二选一」的两个可空字段。

    strict schema 下两个可空字段要靠入口再校验「恰好一个」，模型也更容易填错；种类由工具按 ``http(s)://``
    前缀与磁盘事实判定。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    target: str = Field(
        min_length=1,
        description=(
            "What to show: a path relative to the workspace root, an absolute path for something "
            "outside it ('~' is not expanded), or a web address starting with http:// or https://. "
            "Paths may point to a file or a directory."
        ),
    )
    caption: str | None = Field(
        default=None,
        description=(
            'One line of context the user sees when hovering the card, e.g. "the failing case is '
            'row 42". Pass null if the item speaks for itself.'
        ),
    )


class PresentDeliverablesArgs(BaseModel):
    """定义 present_deliverables 的参数约束；字段说明面向调用该工具的 Agent。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    items: list[DeliverableTarget] = Field(
        min_length=1,
        max_length=PRESENT_DELIVERABLES_MAX,
        description=f"Things to show to the user (at most {PRESENT_DELIVERABLES_MAX}).",
    )
