"""Conversation Transport 的 Run 内 message 契约。"""

from typing import Literal

from typing_extensions import TypedDict

from app.assistant_transport.state.conversation_state_part import ConversationStatePart
from app.models.task_message import Delivery


class ConversationStateMessage(TypedDict):
    """一条 Run 内的 user/assistant 消息。

    消息不重复保存 Run 标识或 Run 终态。消息归属由外层
    ``ConversationRunSnapshot.runId`` 确定，消息生命周期由外层 Run 状态派生；只有
    ``parts`` 保留各自的流式与工具执行生命周期。
    """

    id: str
    role: Literal["user", "assistant"]
    parts: list[ConversationStatePart]


class ConversationQueuedMessage(ConversationStateMessage):
    """尚未被模型边界消费、需投影的排队消息。

    ``delivery`` 只供前端区分「下个模型边界并入」与「交给下一个 Run」，事实仍在外部队列。
    """

    delivery: Delivery
