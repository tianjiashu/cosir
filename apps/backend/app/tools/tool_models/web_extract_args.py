"""web_extract 工具的参数模型。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WebExtractArgs(BaseModel):
    """网页正文提取工具的模型可见参数。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    urls: list[object] = Field(
        min_length=1,
        description="URL strings or search result objects with url/href.",
    )
    format: Literal["markdown", "html", "text"] = Field(
        default="markdown",
        description="Desired extraction format.",
    )
    char_limit: int | None = Field(
        default=None,
        ge=1,
        description="Maximum characters per page returned to the model.",
    )
