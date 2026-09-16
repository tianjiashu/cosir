"""Conversation Run 终态持久化的 token 用量类型。"""

from typing_extensions import TypedDict


class ConversationRunUsage(TypedDict):
    """Persisted six-field token usage contract for one run."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int | None
    reasoning_tokens: int


__all__ = ["ConversationRunUsage"]
