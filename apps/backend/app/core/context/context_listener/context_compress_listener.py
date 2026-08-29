"""压缩扩展点在 listener 链上的占位订阅者。

以 ``order=0`` 占据 listener 链的最早执行位，为 context compaction 预留挂载点：真实
压缩策略落地后，在此消费 ``event.entries``（含 turn 归属，便于按 turn 切分与保留
system / 近期上下文）。当前无任何压缩算法，故 :meth:`listen` 为空实现——不为臆想需求
提前写死策略（YAGNI）。
"""

from app.core.context.context_listener.context_listener import ContextListener
from app.core.context.context_listener.listener_event import ListenerEvent
from app.core.context.context_listener.listener_result import ListenerResult


class ContextCompressListener(ContextListener):
    main_agent_only = False
    order = 0

    def __init__(self) -> None:
        """构造占位订阅者（无状态）。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """

    def listen(self, event: ListenerEvent, result: ListenerResult) -> None:
        """接收上下文变化事件但不执行任何动作（压缩占位）。

        参数:
            event: 监听事件（含事件类型、变更条目、已有占用与窗口上限）。
            result: 累计结果；本实现不修改它。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """
