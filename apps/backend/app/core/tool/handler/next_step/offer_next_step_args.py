"""``offer_next_step`` 工具的参数模型；字段 description 会进入模型可见的工具 schema。

长度上限（问题与每个按钮标签）连同去空白后的非空检查一起在工具入口校验，不拆成 schema 与入口两处。
"""

from pydantic import BaseModel, ConfigDict, Field

NEXT_STEP_ASK_MAX = 100
NEXT_STEP_REPLY_MAX = 24


class OfferNextStepArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    ask: str = Field(
        min_length=1,
        description=(
            "The one decision, in one short sentence (at most "
            f"{NEXT_STEP_ASK_MAX} characters). Never two decisions joined by \"and\". No "
            "reason, no recommendation, and do not list the options the buttons give."
        ),
    )
    replies: list[str] = Field(
        min_length=1,
        max_length=2,
        description=(
            "1-2 buttons, your recommendation first. Each label is the user's words back to "
            f"you, 1-3 words (at most {NEXT_STEP_REPLY_MAX} characters), such as \"Yes, fix it\" "
            "or \"Not now\". Add a second button only when it is a realistic alternative."
        ),
    )
