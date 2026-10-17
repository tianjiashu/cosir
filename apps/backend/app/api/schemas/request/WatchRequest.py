"""文件监听订阅请求体。"""

from pydantic import BaseModel, Field


class WatchRequest(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=4096)
