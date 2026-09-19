"""Conversation Transport 的稳定错误结构契约。"""

from typing_extensions import TypedDict


class ConversationStateError(TypedDict):
    """面向 UI 的稳定错误结构。

    只包含稳定的机器可读 ``code`` 与面向用户的安全 ``message``。``retryable`` 是工具观察的
    字段（仅面向模型），不属于 Transport 错误契约。
    """

    code: str
    message: str
