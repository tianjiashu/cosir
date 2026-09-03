"""ConversationState 推理 part 的中性 JSON 契约。"""

from typing import Literal, NotRequired, TypedDict


class ConversationStateReasoningPart(TypedDict):
    """推理消息 part 的中性投影。"""

    type: Literal["reasoning"]
    text: str
    status: NotRequired[str]
