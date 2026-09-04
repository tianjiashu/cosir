"""可增量追加的文本 part 契约。"""

from typing import Literal, NotRequired, TypedDict


class ConversationStateTextPart(TypedDict):
    """可增量追加的文本 part。

    表达一条消息中可流式追加的纯文本片段，status 标记当前增量阶段。
    """

    type: Literal["text"]
    text: str
    status: NotRequired[Literal["running", "completed"]]
