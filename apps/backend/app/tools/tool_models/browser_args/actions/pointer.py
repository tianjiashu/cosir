"""指针动作：点击、悬停、拖放，以及它们共用的落点。

元素用快照里的 ref 指定，也可以给视口坐标覆盖快照里没有的东西（画布、地图、滑块上的某一点）；
坐标与视口截图的像素一致（截图按 CSS 像素出图）。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.tools.tool_models.browser_args._shared import (
    MODIFIERS,
    POINT_REF_DESCRIPTION,
    REF_PATTERN,
    X_FIELD,
    Y_FIELD,
    one_point,
)


class ClickAction(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["click"]
    ref: str | None = Field(default=None, pattern=REF_PATTERN, description=POINT_REF_DESCRIPTION)
    x: float | None = X_FIELD
    y: float | None = Y_FIELD
    button: Literal["left", "right"] = Field(
        default="left", description="right opens the page's context menu."
    )
    click_count: Literal[1, 2] = Field(
        default=1, description="2 for a double click, e.g. to select a word or open an item."
    )
    modifiers: MODIFIERS | None = Field(
        default=None,
        max_length=4,
        description=(
            "Keys held during the click, e.g. Shift to extend a selection or ControlOrMeta to "
            "add to it. ControlOrMeta is Cmd on macOS and Ctrl elsewhere."
        ),
    )

    @model_validator(mode="after")
    def _point(self) -> "ClickAction":
        one_point(self.ref, self.x, self.y)
        return self


class HoverAction(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["hover"]
    ref: str | None = Field(
        default=None,
        pattern=REF_PATTERN,
        description=(
            "Element to move the mouse over, e.g. to open a menu that shows on hover. Leave "
            "null when using x and y."
        ),
    )
    x: float | None = X_FIELD
    y: float | None = Y_FIELD

    @model_validator(mode="after")
    def _point(self) -> "HoverAction":
        one_point(self.ref, self.x, self.y)
        return self


class BrowserPoint(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    ref: str | None = Field(default=None, pattern=REF_PATTERN, description=POINT_REF_DESCRIPTION)
    x: float | None = X_FIELD
    y: float | None = Y_FIELD

    @model_validator(mode="after")
    def _point(self) -> "BrowserPoint":
        one_point(self.ref, self.x, self.y)
        return self


# 按住左键从 start 移到 end 再松开：拖放排序、滑块、在画布上画线。两端都要在视口里；ref 的那端
# 会先滚入视野，可能把另一端滚走，所以离得远时先 scroll 好再用坐标。
class DragAction(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["drag"]
    start: BrowserPoint = Field(description="Where to press the mouse button.")
    end: BrowserPoint = Field(description="Where to release it.")
