"""Conversation Transport 的稳定错误结构契约。"""

from typing_extensions import TypedDict


class ConversationStateError(TypedDict):
    """面向 UI 的 Run 错误结构。

    ``code`` 是内部稳定类别；``message`` 可以是模型 HTTP 响应体直接提供的消息。Transport
    只承载这两个字段，不透传完整响应体。``retryable`` 是工具观察的字段，不属于此契约。
    """

    code: str
    message: str
