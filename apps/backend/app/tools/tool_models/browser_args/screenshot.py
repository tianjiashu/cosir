"""browser_screenshot：截图给模型看。"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.tools.tool_models.browser_args._shared import REF_PATTERN, tab_field


class BrowserRegion(BaseModel):
    """视口里的一块（CSS 像素，与视口截图的像素、动作坐标同一坐标系）；是否落在视口内由宿主按几何判断。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    x: float = Field(ge=0, allow_inf_nan=False, description="Left edge in viewport CSS pixels.")
    y: float = Field(ge=0, allow_inf_nan=False, description="Top edge in viewport CSS pixels.")
    width: float = Field(gt=0, allow_inf_nan=False, description="Width in CSS pixels.")
    height: float = Field(gt=0, allow_inf_nan=False, description="Height in CSS pixels.")


# 截哪里：视口（默认）、整页、单个元素（看清图标、图片、按钮、验证码），或视口里的一块（元素内部的
# 一部分：画布上画的图表、大表格的局部）。后两种图更小、省上下文。
class BrowserScreenshotArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    full_page: bool = Field(
        default=False, description="Capture the whole page (height capped) instead of the viewport."
    )
    ref: str | None = Field(
        default=None,
        pattern=REF_PATTERN,
        description=(
            "Capture only this element (with a small margin; small ones are enlarged), e.g. an "
            "icon, an image, a button or a captcha. Null for the viewport, the whole page or a "
            "region."
        ),
    )
    region: BrowserRegion | None = Field(
        default=None,
        description=(
            "Capture only this part of the viewport, e.g. a chart drawn on a canvas or part of a "
            "large table; take its coordinates from a viewport screenshot. It must lie "
            "inside the viewport (scroll first); small regions are enlarged. Null otherwise."
        ),
    )
    tab: str | None = tab_field()

    @model_validator(mode="after")
    def _one_area(self) -> "BrowserScreenshotArgs":
        if sum((self.full_page, self.ref is not None, self.region is not None)) > 1:
            raise ValueError("give at most one of full_page, ref and region")
        return self
