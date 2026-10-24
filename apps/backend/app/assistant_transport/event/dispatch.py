"""状态事实在事务提交后同步投影，与模型和工具增量共用 projector 的串行边界。"""

from typing import Literal

from app.core.events import WorkflowEvent

DispatchChannel = Literal["direct"]


def dispatch_conversation_event(event: WorkflowEvent) -> DispatchChannel:
    """不探测执行框架上下文；所有事件经过同一投影入口。"""
    from app.service.depends import get_conversation_event_projector

    get_conversation_event_projector().process(event)
    return "direct"
