"""Assistant Transport 的可变 state 快照类型。

单一职责：在 API 适配边界为 Assistant Transport 的 state 形状提供命名。

职责边界：
- 负责：以 Assistant Transport 命名复用 service 侧的中性 state 投影类型。
- 不负责：定义形状本身（由 ``app.service.task.conversation_state_snapshot`` 单一持有），
  也不负责 persistence 事实、assistant-ui 类型或 Agent Runtime 语义。

这些类型只约束传输边界的 JSON 形状；运行时值仍是普通 ``dict`` / ``list``，以便
assistant-stream 的 StateProxy 生成 ``set`` 与 ``append-text`` 操作。
"""

from app.service.task.conversation_state_snapshot import (
    ConversationStateError,
    ConversationStateMessage,
    ConversationStatePart,
    ConversationStateRun,
    ConversationStateSnapshot,
)

AssistantTransportError = ConversationStateError
AssistantTransportMessage = ConversationStateMessage
AssistantTransportPart = ConversationStatePart
AssistantTransportRun = ConversationStateRun
AssistantTransportState = ConversationStateSnapshot

__all__ = [
    "AssistantTransportError",
    "AssistantTransportMessage",
    "AssistantTransportPart",
    "AssistantTransportRun",
    "AssistantTransportState",
]
