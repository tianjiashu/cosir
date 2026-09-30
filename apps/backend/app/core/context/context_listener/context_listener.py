"""运行时上下文变化订阅协议。

只定义 ``ContextListener`` 协议，供 ``RuntimeContextManager`` 在消息变化时通知
订阅者，使「上下文变了」这一事实能由 ``core/context`` 层独立表达。
"""

from __future__ import annotations

from typing import Protocol

from app.core.context.context_listener.listener_event import ListenerEvent


class ContextListener(Protocol):
    """订阅运行时上下文变化，在有效上下文变更后收到通知。

    实现方决定是否执行上下文变化旁路动作；manager 不感知实现细节，仅在
    ``mark_context_changed`` 时按 ``order`` 依次通知所有已订阅 listener。
    """

    # 是否只允许主 Agent 订阅
    main_agent_only: bool
    # 订阅顺序
    order: int

    def listen(self, event: ListenerEvent) -> None:
        """接收上下文变化事件。

        参数:
            event: 监听事件；条目含 turn 归属，监听器可按需从 ``entry.message`` 派生。

        返回:
            无。
        """
        ...
