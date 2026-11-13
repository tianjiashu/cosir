"""执行事件的消费回调：core 交出事件的唯一出口（依赖倒置，core 不导入 ``assistant_transport``）。

执行内核与调度只认这个回调，不知道事件被投影到哪里：对话场景由 lifespan 装配时注入 Assistant
Transport projector 的 ``process``（测试由 conftest 注入）。未注入就发事件属于装配缺陷，直接报错，
不静默丢弃。事件是旁路，返回值内核不使用。
"""

from collections.abc import Callable

from app.core.events.workflow_event import WorkflowEvent

EventSink = Callable[[WorkflowEvent], object]
"""接收执行事件的回调。"""

_sink: EventSink | None = None


def set_event_sink(sink: EventSink | None) -> None:
    """装配期注入进程级消费方；``None`` 解除（进程关闭后）。"""
    global _sink
    _sink = sink


def event_sink() -> EventSink:
    """取已注入的消费方。

    异常:
        RuntimeError: 尚未装配（lifespan 之外直接调用 core 而没有注入）。
    """
    if _sink is None:
        raise RuntimeError("event sink is not assembled")
    return _sink
