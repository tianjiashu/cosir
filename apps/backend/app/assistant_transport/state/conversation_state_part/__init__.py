"""中性 ConversationState part 契约。"""

from app.assistant_transport.state.conversation_state_part.reasoning_part import ConversationStateReasoningPart
from app.assistant_transport.state.conversation_state_part.text_part import ConversationStateTextPart
from app.assistant_transport.state.conversation_state_part.tool_call_part import (
    ConversationStateToolApproval,
    ConversationStateToolCallPart,
    ConversationStateToolError,
)

ConversationStatePart = (
    ConversationStateTextPart
    | ConversationStateReasoningPart
    | ConversationStateToolCallPart
)

__all__ = [
    "ConversationStatePart",
    "ConversationStateReasoningPart",
    "ConversationStateTextPart",
    "ConversationStateToolApproval",
    "ConversationStateToolCallPart",
    "ConversationStateToolError",
]
