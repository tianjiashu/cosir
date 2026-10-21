"""Assistant Transport 侧的事件类型与分发。

执行内核产出的事实事件定义在 ``app.core.events``；本包只追加 Transport 自有的用户输入展示事件，
并给出投影接受的判别式联合与同步分发入口。事件到 snapshot mutation 的投影见
``app.assistant_transport.projection``。
"""

from app.assistant_transport.event.conversation_event import ConversationEvent
from app.assistant_transport.event.user_input_event import UserInputAppendedEvent, UserInputPart

__all__ = [
    "ConversationEvent",
    "UserInputAppendedEvent",
    "UserInputPart",
]
