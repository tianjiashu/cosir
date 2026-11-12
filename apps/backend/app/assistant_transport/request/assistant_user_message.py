"""Assistant UI 用户消息的 wire schema：文本 part 与图片 part。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AssistantTextPart(BaseModel):
    """文本 part；首尾空白去掉，空白文本在这里拒绝（空输入的唯一校验点）。"""

    model_config = ConfigDict(extra="ignore")

    type: Literal["text"]
    text: str

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message text must not be blank")
        return normalized


class AssistantImagePart(BaseModel):
    """图片 part：只收项目自有的附件定位符（由上传接口发布）。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["image"]
    image: str = Field(pattern=r"^cosir-attachment://[0-9a-f]{64}$")


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

    role: Literal["user"]
    parts: list[AssistantUserPart] = Field(min_length=1, max_length=32)
