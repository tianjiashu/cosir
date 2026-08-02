"""web_extract 工具的参数模型。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WebExtractArgs(BaseModel):
    """网页正文提取工具的模型可见参数。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    urls: list[object] = Field(
        min_length=1,
        max_length=5,
        description="List of URLs to extract content from (max 5 URLs per call).",
    )
    format: Literal["markdown", "html", "text"] = Field(
        default="markdown",
        description=(
            "Desired extraction format. Use markdown or html; text is a fallback "
            "when the provider does not support structured formats."
        ),
    )
    char_limit: int | None = Field(
        default=None,
        ge=2000,
        description=(
            "Optional per-page character budget hint passed to the extraction provider "
            "(default 15000). The full page text is always returned to the model; when the "
            "combined output exceeds the global tool output budget it is auto-truncated and "
            "the full text is saved to disk with a [output truncated; full output: <path>] "
            "marker you can read_file. Raise this only if your provider uses it for its own "
            "pre-truncation."
        ),
    )
