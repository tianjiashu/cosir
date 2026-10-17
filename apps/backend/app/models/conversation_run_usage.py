"""Conversation Run 持久化的 token 用量类型。"""

from typing_extensions import TypedDict


class ConversationRunUsage(TypedDict):
    """一次 Run 的 provider token 用量；缺失明细保留为 None。"""

    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cache_hit_tokens: int | None
    cache_miss_tokens: int | None
    reasoning_tokens: int | None


__all__ = ["ConversationRunUsage"]
