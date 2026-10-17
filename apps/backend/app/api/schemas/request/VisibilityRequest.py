"""可见性设置请求体。"""

from pydantic import BaseModel, Field


class VisibilityRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=1000)
    ignored: bool
