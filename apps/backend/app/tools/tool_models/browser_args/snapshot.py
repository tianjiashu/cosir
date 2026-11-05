"""browser_snapshot：读页面的可访问性大纲。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.tools.tool_models.browser_args._shared import tab_field


# 快照的视图：整页、只看视口、按关键词查找（两者可同时用），宿主按同一份大纲渲染，ref 是同一套。
class BrowserSnapshotArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    tab: str | None = tab_field()
    scope: Literal["page", "viewport"] = Field(
        default="page",
        description=(
            "viewport to read only what is on screen now (with the containers around it), e.g. "
            "after scrolling; page for the whole page."
        ),
    )
    find: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "Only lines containing this text (ignoring case), with the containers around them, "
            "e.g. a button label or a product name. Null for no filter."
        ),
    )
    from_line: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Line to start from when a long snapshot was cut off; the cut-off note gives the "
            "number. Pass null to read from the top."
        ),
    )
