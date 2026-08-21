from enum import Enum
from app.models import RuntimeMessage

class ContextEventType(str, Enum):
    """事件类型。"""
    ADD_MESSAGE = "add_message"
    LOAD_HISTORY = "load_history"
    CONTEXT_COMPRESSED = "context_compressed"


class ListenerEvent:
    """监听事件基类。"""

    type: ContextEventType
    messages: list[RuntimeMessage]
    usage: int
    total_tokens: int


    def __init__(self, type: ContextEventType, messages: list[RuntimeMessage], usage: int, total_tokens: int) -> None:
        self.type = type
        self.messages = messages
