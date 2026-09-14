from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


# text part：用户输入的文本内容块；
# image part：图片内容块。
class AssistantTextPart(BaseModel):
    """校验 Assistant UI 用户消息中的文本 part。"""

    model_config = ConfigDict(extra="ignore")

    type: Literal["text"]
    text: str

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        """拒绝空白文本。

        参数:
            value: 用户消息中的文本。

        返回:
            去除首尾空白后的文本。

        异常:
            ValueError: 当文本为空白时抛出。

        副作用:
            无。
        """
        normalized = value.strip()
        if not normalized:
            raise ValueError("message text must not be blank")
        return normalized
