"""回合的可见输入与回复判定；写入期回合归属和冷重建共用。"""

from openai.types.chat import ChatCompletionMessageParam

from app.models.message_context.message_metadata.base.message_kinds import TURN_INPUT_KINDS
from app.models.message_context.message_metadata.base.message_metadata_base import MessageMetadata
from app.models.message_context.message_metadata.user_message_metadata import UserMessageMetadata
from app.utils.message_content import content_to_text


def is_turn_input(
    message: ChatCompletionMessageParam, metadata: MessageMetadata, project: bool
) -> bool:
    """可见的一轮输入（用户输入或 Agent 消息）；隐藏的输入不在前端开回合，也就不算。"""
    return (
        message["role"] == "user"
        and isinstance(metadata, UserMessageMetadata)
        and metadata.kind in TURN_INPUT_KINDS
        and project
    )


def is_visible_reply(message: ChatCompletionMessageParam) -> bool:
    """有正文或调用的 assistant 行：实时投影只在这时建出回复（见 ``ensure_reply``）。"""
    return message["role"] == "assistant" and bool(
        content_to_text(message.get("content")) or message.get("tool_calls")
    )
