"""browser_tabs：列、选、关本 Session 的标签。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# list 不需要 tab；select / close 需要。
class BrowserTabsArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    action: Literal["list", "select", "close"] = Field(
        description=(
            "list this session's tabs, select one as the current tab, or close one you no longer need."
        )
    )
    tab: str | None = Field(
        default=None, pattern=r"^t\d+$", description="Tab id for select or close; null for list."
    )

    @model_validator(mode="after")
    def _tab_for_select_and_close(self) -> "BrowserTabsArgs":
        if self.action != "list" and self.tab is None:
            raise ValueError(f"{self.action} needs a tab")
        return self
