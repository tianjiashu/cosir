from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.assistant_transport.request.part import (
    AssistantFileAttachment,
    AssistantImagePart,
    AssistantTextPart,
)

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
    # assistant-ui 当前不会把普通 file content 转换进 command；桌面端在 HTTP 边界
    # 显式补充此字段，后端据此把 token 与本机路径绑定。
    # 绑定随输入存入 message_queue，消费后转入 context。
    attachments: list[AssistantFileAttachment] = Field(default_factory=list, max_length=32)
