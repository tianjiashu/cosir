"""Assistant Transport 图片 part。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AssistantImagePart(BaseModel):
    """校验用户消息中的 project-owned 图片 locator。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["image"]
    image: str = Field(pattern=r"^cosir-attachment://[0-9a-f]{64}$")
