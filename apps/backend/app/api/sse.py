"""运行时事件的 SSE 格式化辅助工具。"""

import json

from app.events.types import RuntimeEvent


def format_sse_event(event: RuntimeEvent) -> str:
    """将一个运行时事件格式化为 SSE 传输消息。

    参数:
        event: 待序列化的运行时事件。

    返回:
        使用 ``event:`` 和 ``data:`` 字段的 Server-Sent Event 字符串。

    异常:
        TypeError: 当事件 payload 不可 JSON 序列化时抛出。

    副作用:
        无。
    """

    return f"event: {event.event_type}\ndata: {json.dumps(event.to_dict())}\n\n"
