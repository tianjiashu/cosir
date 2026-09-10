"""Conversation Transport 的 Run 内 message 契约。"""

from typing import Literal

from typing_extensions import TypedDict

from app.assistant_transport.state.conversation_state_part import ConversationStatePart


class ConversationStateMessage(TypedDict):
    """一条 Run 内的 user/assistant 消息。

    消息不重复保存 Run 标识或 Run 终态。消息归属由外层
    ``ConversationRunSnapshot.runId`` 确定，消息生命周期由外层 Run 状态派生；只有
    ``parts`` 保留各自的流式与工具执行生命周期。
    """

    id: str
    role: Literal["user", "assistant"]
    parts: list[ConversationStatePart]
