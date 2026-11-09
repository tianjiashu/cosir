from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.assistant_transport.request.part import AssistantImagePart, AssistantTextPart

AssistantUserPart = Annotated[
    AssistantTextPart | AssistantImagePart,
    Field(discriminator="type"),
]


class AssistantUserMessage(BaseModel):
    """校验 Assistant UI 的用户消息。

    本机文件与工作区引用是正文里的 ``<file>`` / ``<ref>`` 胶囊，不再有独立的附件字段：
    胶囊正文由 composer 直接写进 text part。
    """

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    role: Literal["user"]
    parts: list[AssistantUserPart] = Field(min_length=1, max_length=32)
