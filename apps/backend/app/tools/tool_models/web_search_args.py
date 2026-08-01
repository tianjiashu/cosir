"""web_search 工具的参数模型。"""

from pydantic import BaseModel, ConfigDict, Field


class WebSearchArgs(BaseModel):
    """网页搜索工具的模型可见参数。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    query: str = Field(min_length=1, description="Search query.")
    limit: int = Field(default=5, ge=1, description="Maximum search results to return.")
