"""文件内文本搜索请求体。"""

from pydantic import BaseModel, Field


class TextSearchRequest(BaseModel):
    path: str
    query: str = Field(min_length=1, max_length=512)
    encoding: str
    version: str
    offset: int = Field(default=0, ge=0)
