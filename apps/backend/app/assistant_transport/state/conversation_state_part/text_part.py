"""ConversationState 文本 part 的中性 JSON 契约。"""

from typing import Literal, NotRequired, TypedDict


class ConversationStateTextPart(TypedDict):
    """文本消息 part 的中性投影。"""

    type: Literal["text"]
    text: str
    status: NotRequired[str]
