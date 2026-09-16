"""Conversation Transport 普通文件 part。"""

from typing import Literal

from typing_extensions import TypedDict


class ConversationStateFilePart(TypedDict):
    """只包含稳定本机 locator 与展示元数据的普通文件 part。"""

    type: Literal["file"]
    file: str
    name: str
    contentType: str
