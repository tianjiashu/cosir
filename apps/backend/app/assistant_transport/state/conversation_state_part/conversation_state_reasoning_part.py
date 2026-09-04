"""可增量追加的 reasoning part 契约。"""

from typing import Literal, NotRequired, TypedDict


class ConversationStateReasoningPart(TypedDict):
    """可增量追加的 reasoning part。

    表达一条消息中可流式追加的模型推理/思考片段，status 标记当前增量阶段。
    """

    type: Literal["reasoning"]
    text: str
    status: NotRequired[Literal["running", "completed"]]
