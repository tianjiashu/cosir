"""web_search 工具的参数模型。"""

from pydantic import BaseModel, ConfigDict, Field

from app.config.settings import Settings


class WebSearchArgs(BaseModel):
    """网页搜索工具的模型可见参数。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    query: str = Field(
        min_length=1,
        description=(
            "Search query to look up on the web. You may include backend-supported "
            "operators such as site:example.com, filetype:pdf, intitle:word, -term, "
            'or "exact phrase".'
        ),
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=Settings.WEB_SEARCH_LIMIT_MAX,
        description=(
            f"Maximum number of results to return. Defaults to 5, hard-capped by "
            f"the runtime limit ({Settings.WEB_SEARCH_LIMIT_MAX})."
        ),
    )
