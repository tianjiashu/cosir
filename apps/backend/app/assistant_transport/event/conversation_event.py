"""Assistant Transport 投影接受的事件判别式联合。

在执行内核的 ``WorkflowEvent`` 基础上追加 Transport 自有的 ``UserInputAppendedEvent``。
只承载类型联合，不含事件定义与投影逻辑（投影见 ``assistant_transport.projection``）。
"""

from typing import Annotated

from pydantic import Field

from app.assistant_transport.event.user_input_event import UserInputAppendedEvent
from app.core.events import (
    AssistantPartClosedEvent,
    AssistantTextDeltaEvent,
    RunInitializedEvent,
    RunResultUpdatedEvent,
    RunStatusChangedEvent,
    ToolCallCreatedEvent,
    ToolCallRuntimeUpdateEvent,
    ToolCallsSettledEvent,
    ToolCallStatusChangedEvent,
)

# 按「产生顺序」排列：run 建立 → 用户输入 → 状态 → assistant 输出 → 工具。
ConversationEvent = Annotated[
    RunInitializedEvent
    | UserInputAppendedEvent
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
"""任意一条可投影到 Transport snapshot 的事件；``type`` 字段为判别式。"""
