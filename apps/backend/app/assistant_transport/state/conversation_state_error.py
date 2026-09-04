"""Conversation Transport 的稳定错误结构契约。"""

from typing import TypedDict


class ConversationStateError(TypedDict):
    """面向 UI 的稳定错误结构。"""

    code: str
    message: str
    retryable: bool
