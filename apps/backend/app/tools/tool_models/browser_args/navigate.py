"""browser_navigate：打开地址，或在当前标签里后退 / 前进 / 刷新。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.tools.tool_models.browser_args._shared import tab_field


# Agent 固定的视口：宿主经 CDP 设备模拟按这个尺寸排版，面板只等比缩放显示；范围与宿主的模拟能力
# 一致。不固定时视口跟随面板（宿主 ``TabEmulation``）。
class BrowserViewport(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    width: int = Field(ge=320, le=3840, description="Viewport width in CSS pixels.")
    height: int = Field(ge=240, le=2160, description="Viewport height in CSS pixels.")
    mobile: bool = Field(
        default=False,
        description="Emulate a phone: mobile meta viewport and touch input.",
    )


# url 与 action 二选一：打开地址，或在当前标签里后退 / 前进 / 刷新。
class BrowserNavigateArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    url: str | None = Field(
        default=None,
        max_length=4096,
        description=(
            "http(s) URL, absolute local file path or file:// URL to open. Leave null when "
            "using action."
        ),
    )
    action: Literal["back", "forward", "reload"] | None = Field(
        default=None,
        description=(
            "History action on the tab instead of opening a URL. Leave null when using url."
        ),
    )
    tab: str | None = tab_field()
    new_tab: bool = Field(
        default=False,
        description="Open the URL in a new tab of this task; tab is ignored when true.",
    )
    viewport: BrowserViewport | Literal["auto"] | None = Field(
        default=None,
        description=(
            "Fix the tab's viewport before loading, e.g. 390x844 mobile to check a phone layout; "
            "it stays until changed again. auto goes back to the default: the viewport follows "
            "the size of the user's browser panel. null keeps the current one."
        ),
    )
    color_scheme: Literal["light", "dark", "auto"] | None = Field(
        default=None,
        description=(
            "Emulate the page's prefers-color-scheme before loading; auto follows the app's "
            "appearance. It stays until changed again; null keeps the current one."
        ),
    )

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "BrowserNavigateArgs":
        if (self.url is None) == (self.action is None):
            raise ValueError("provide exactly one of url or action")
        if self.new_tab and self.url is None:
            raise ValueError("new_tab needs a url")
        return self
