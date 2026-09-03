from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CustomCommand(BaseModel):
    """保留 Assistant Transport 扩展命令的显式、可审计边界。"""

    model_config = ConfigDict(extra="allow")

    type: Literal["custom"]
    commandId: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    payload: object | None = None
