"""执行事件的消费回调。"""

from collections.abc import Callable

from app.core.events.workflow_event_envelope import WorkflowEventEnvelope

EventSink = Callable[[WorkflowEventEnvelope], object]
"""接收执行事件的回调。

执行内核只认这个回调，不知道事件被投影到哪里：对话场景由装配方传入 Assistant Transport
projector 的 ``process``，core 因此不依赖 ``assistant_transport``。事件是旁路，返回值内核不使用。
"""
