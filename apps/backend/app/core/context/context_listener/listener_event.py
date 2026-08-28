from dataclasses import dataclass
from enum import Enum

from app.models import RuntimeMessage


class ContextEventType(str, Enum):
    """事件类型。"""

    ADD_MESSAGE = "add_message"
    LOAD_HISTORY = "load_history"
    CONTEXT_COMPRESSED = "context_compressed"


@dataclass(frozen=True)
class ListenerEvent:
    """运行时上下文变化事件。"""

    type: ContextEventType
    messages: list[RuntimeMessage]
    usage: int
    total_tokens: int
    allow_write_event_failure: bool = False
