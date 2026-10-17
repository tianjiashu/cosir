"""文件搜索请求体。"""

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    include_directories: bool = False
    query: str = Field(min_length=1, max_length=512)
    offset: int = Field(default=0, ge=0)
