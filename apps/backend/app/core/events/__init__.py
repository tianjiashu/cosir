"""执行内核产出的事实事件与消费回调。

事件只描述「已经发生了什么」，不含投影逻辑；消费方（Assistant Transport projector 等）经
``EventSink`` 回调接收并自行决定如何呈现。

使用约定：

- 事件描述**已发生的事实**，不是待执行的意图；领域侧完成仲裁与落库后才发出。
- 除 ``*_delta`` 外，事件必须可按业务键（``run_id`` / ``tool_call_id``）重复投递。
"""

from app.core.events.event_sink import EventSink
from app.core.events.message_event import (
    AssistantPartClosedEvent,
    AssistantTextDeltaEvent,
    AssistantTextPartKind,
)
from app.core.events.run_event import (
    RunInitializedEvent,
    RunResultUpdatedEvent,
    RunStatusChangedEvent,
)
from app.core.events.task_event import TaskTitleUpdatedEvent
from app.core.events.tool_call_event import (
    ToolCallCreatedEvent,
    ToolCallsSettledEvent,
    ToolCallStatusChangedEvent,
)
from app.core.events.tool_runtime_event import (
    TerminalOutputDeltaData,
    ToolCallRuntimeUpdateEvent,
)
from app.core.events.user_input_event import UserInputConsumedEvent
from app.core.events.workflow_event import WorkflowEvent
from app.core.events.workflow_event_envelope import WorkflowEventEnvelope

__all__ = [
    "AssistantPartClosedEvent",
    "AssistantTextDeltaEvent",
    "AssistantTextPartKind",
    "EventSink",
    "RunInitializedEvent",
    "RunResultUpdatedEvent",
    "RunStatusChangedEvent",
    "TaskTitleUpdatedEvent",
    "TerminalOutputDeltaData",
    "ToolCallCreatedEvent",
    "ToolCallRuntimeUpdateEvent",
    "ToolCallStatusChangedEvent",
    "ToolCallsSettledEvent",
    "UserInputConsumedEvent",
    "WorkflowEvent",
    "WorkflowEventEnvelope",
]
