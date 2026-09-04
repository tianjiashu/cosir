from dataclasses import dataclass
from enum import Enum

from app.core.context.context_entry import ContextEntry


class ContextEventType(str, Enum):
    """事件类型。"""

    ADD_MESSAGE = "add_message"
    LOAD_HISTORY = "load_history"
    CONTEXT_COMPRESSED = "context_compressed"


@dataclass(frozen=True)
class ListenerEvent:
    """运行时上下文变化事件。

    以**上下文条目**而非裸消息作为变更载体：``ContextEntry`` 除 LangChain 消息
    外还携带 ``run_id`` 归属，压缩等需要按 run 切分/保留上下文的 listener 才能工作。
    只需消息的 listener 自行从条目派生消息，事件不维护两份等价快照（避免二者不同步）。

    职责边界：只承载「发生了什么变化」的只读快照；不负责变更的落库，也不接受
    listener 反向替换上下文（压缩结果回写属另一条通道，尚未定义）。

    参数:
        type: 变化来源。
        entries: 变化后的有效上下文条目快照（由分发方深拷贝，listener 可安全读取）。
        usage: 分发时记录的上下文已用 token。
        total_tokens: 当前上下文窗口上限。
        allow_write_event_failure: 是否允许事件写入器不可用时继续执行。

    返回:
        不可变的上下文变化事件。

    异常:
        无。

    副作用:
        无。
    """

    type: ContextEventType
    entries: list[ContextEntry]
    usage: int
    total_tokens: int
