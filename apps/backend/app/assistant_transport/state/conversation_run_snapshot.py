"""Conversation Transport 的单个 Run snapshot 契约。"""

from typing_extensions import TypedDict

from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_usage import ConversationStateUsage


class ConversationRunSnapshot(TypedDict):
    """一个 Run 的完整 UI 投影。

    ``runId`` 是内部消息、tool part 和 Run usage 的共同归属键；Run 的状态、终态原因、
    消息及累计模型 usage 必须在同一对象内保持一致。
    """

    runId: int
    status: str
    endReason: str | None
    messages: list[ConversationStateMessage]
    usage: ConversationStateUsage | None
