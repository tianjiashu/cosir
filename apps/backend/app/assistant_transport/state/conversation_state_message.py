"""Conversation Transport 的 message 契约。"""

from typing import Literal, NotRequired

from typing_extensions import TypedDict

from app.assistant_transport.state.conversation_state_part import ConversationStatePart


class ConversationStateMessage(TypedDict):
    """一条面向 Transport 的 user/assistant 消息。"""

    id: str
    runId: NotRequired[int | None]
    role: Literal["user", "assistant"]
    status: str
    endReason: str | None
    parts: list[ConversationStatePart]
