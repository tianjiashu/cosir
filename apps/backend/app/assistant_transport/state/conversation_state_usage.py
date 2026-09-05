"""Conversation Transport 的模型用量契约。"""

from typing import TypedDict


class ConversationStateUsage(TypedDict):
    """一次 Task 当前累计的模型 token 用量。"""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int
    reasoning_tokens: int
