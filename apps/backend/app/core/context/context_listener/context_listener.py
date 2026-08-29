"""运行时上下文变化订阅协议。

只定义 ``ContextListener`` 协议，供 ``RuntimeContextManager`` 在消息变化时通知
订阅者，使「上下文变了」这一事实能由 ``core/context`` 层独立表达。
"""

from __future__ import annotations

from typing import Protocol

from app.core.context.context_listener.listener_event import ListenerEvent
from app.core.context.context_listener.listener_result import ListenerResult


class ContextListener(Protocol):
    """订阅运行时上下文变化，在有效上下文变更后收到通知。

    实现方决定是否执行代价高的动作（估算 / 发事件 / 回写）；manager 不感知实现细节，
    仅在 ``mark_context_changed`` 时按 ``order`` 依次通知所有已订阅 listener。
    """

    # 是否只允许主 Agent 订阅
    main_agent_only: bool
    # 订阅顺序
    order: int

    def listen(self, event: ListenerEvent, result: ListenerResult) -> None:
        """上下文已变化，实现方据此更新 ``result``。

        参数:
            event: 监听事件（含事件类型、变更条目、已有占用与窗口上限）。条目含 turn
                归属，只需消息的实现自行从 ``entry.message`` 派生。
            result: 累计结果，实现方可覆写 ``usage``。

        返回:
            无。
        """
        ...
