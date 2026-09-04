"""Conversation Transport 的 part（消息片段）契约集合。

每个 part 类型独立成文件，便于按职责演进与复用。本包统一导出所有 part 类型、
part 联合类型 ConversationStatePart 以及工具调用状态枚举 ToolCallStatus。
"""

from .conversation_state_reasoning_part import ConversationStateReasoningPart
from .conversation_state_text_part import ConversationStateTextPart
from .conversation_state_tool_call_part import ConversationStateToolCallPart, ToolCallStatus

ConversationStatePart = (
    ConversationStateTextPart | ConversationStateReasoningPart | ConversationStateToolCallPart
)

__all__ = [
    "ConversationStatePart",
    "ConversationStateReasoningPart",
    "ConversationStateTextPart",
    "ConversationStateToolCallPart",
    "ToolCallStatus",
]
