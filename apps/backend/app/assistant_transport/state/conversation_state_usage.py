"""Conversation Transport 的模型用量契约。"""

from typing_extensions import TypedDict


class ConversationStateUsage(TypedDict):
    """一次 run 当前累计的模型 token 用量；cache miss 缺少 provider 明细时为 null。"""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int | None
    reasoning_tokens: int
