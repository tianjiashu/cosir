from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.assistant_transport.request.part import AssistantImagePart, AssistantTextPart

AssistantUserPart = Annotated[
    AssistantTextPart | AssistantImagePart,
    Field(discriminator="type"),
]


class AssistantUserMessage(BaseModel):
    """校验 Assistant UI 的用户消息。"""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    role: Literal["user"]
    parts: list[AssistantUserPart] = Field(min_length=1, max_length=32)

    @property
    def has_sendable_part(self) -> bool:
        """返回消息是否包含至少一个可发送的结构化 part。"""
        return any(part.type in {"text", "image"} for part in self.parts)
