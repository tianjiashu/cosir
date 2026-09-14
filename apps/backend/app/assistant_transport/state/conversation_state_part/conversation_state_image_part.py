"""Conversation Transport 用户图片 part。"""

from typing import Literal

from typing_extensions import TypedDict


class ConversationStateImagePart(TypedDict):
    """只携带稳定附件 locator 的图片展示 part，不携带二进制。"""

    type: Literal["image"]
    image: str
