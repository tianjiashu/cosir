from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.assistant_transport.request.part.assistant_text_part import AssistantTextPart


class AssistantUserMessage(BaseModel):
    """校验 Assistant UI 的用户消息。"""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    role: Literal["user"]
    parts: list[AssistantTextPart] = Field(min_length=1, max_length=32)
