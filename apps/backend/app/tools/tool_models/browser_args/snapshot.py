"""browser_snapshot：读页面的可访问性大纲。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.tools.tool_models.browser_args._shared import tab_field


# 快照的视图：投影（带 ref 的大纲或页面正文）、整页或只看视口、按关键词查找（可同时用），宿主按同一次读取
# 渲染，大纲里的 ref 是同一套。正文只用来读内容，体积约为大纲的 1/3 到 1/10。
class BrowserSnapshotArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    tab: str | None = tab_field()
    format: Literal["outline", "text"] = Field(
        default="outline",
        description=(
            "outline for the page structure with refs to act on; text to read the page's content "
            "(articles, docs, search results) as plain text with headings and list items, at a "
            "fraction of the size but without refs or link urls."
        ),
    )
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
