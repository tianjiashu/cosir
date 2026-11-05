"""browser_screenshot：截图给模型看。"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.tools.tool_models.browser_args._shared import REF_PATTERN, tab_field


# 截哪里：视口（默认）、整页，或单个元素（看清图标、图表、验证码，图更小、省上下文）。
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
            "icon, chart or captcha. Null for the viewport or the whole page."
        ),
    )
    tab: str | None = tab_field()

    @model_validator(mode="after")
    def _one_area(self) -> "BrowserScreenshotArgs":
        if self.full_page and self.ref is not None:
            raise ValueError("give full_page or ref, not both")
        return self
