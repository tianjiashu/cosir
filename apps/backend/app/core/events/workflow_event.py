"""执行事件的判别式联合。

只承载「把执行内核产出的事件收成一个可判别、可校验的类型」，不含事件定义本身。它同时是
Assistant Transport 投影接受的全部事件类型。
"""

from typing import Annotated

from pydantic import Field

from app.core.events.message_event import AssistantPartClosedEvent, AssistantTextDeltaEvent
from app.core.events.run_event import (
    RunInitializedEvent,
    RunResultUpdatedEvent,
    RunStatusChangedEvent,
)
from app.core.events.tool_call_event import (
    ToolCallCreatedEvent,
    ToolCallsSettledEvent,
    ToolCallStatusChangedEvent,
)
from app.core.events.tool_runtime_event import ToolCallRuntimeUpdateEvent
from app.core.events.user_input_event import UserInputConsumedEvent

# 按「产生顺序」排列：run 建立 → 用户输入 → 状态 → assistant 输出 → 工具，使本清单同时充当一次
# run 的时间线。
WorkflowEvent = Annotated[
    RunInitializedEvent
    | UserInputConsumedEvent
    | RunStatusChangedEvent
    | RunResultUpdatedEvent
    | AssistantTextDeltaEvent
    | AssistantPartClosedEvent
    | ToolCallCreatedEvent
    | ToolCallStatusChangedEvent
    | ToolCallsSettledEvent
    | ToolCallRuntimeUpdateEvent,
    Field(discriminator="type"),
]
"""执行内核产出的任意一条事件；``type`` 字段为判别式。"""
