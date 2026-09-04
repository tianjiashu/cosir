"""Conversation event 的判别式联合。

本模块只承载「把所有域事件收成一个可判别、可校验的类型」这一单一职责，不含任何事件
定义本身。各域事件按职责分布在其域模块中（``run_event`` / ``message_event`` /
``tool_call_event`` / ``usage_event``），本模块只做汇总。

判别式联合的价值：

- 从 dict / JSON 反序列化时（日志回放、测试、未来的跨进程通道）由 ``type`` 精确路由到
  具体类型，不需要手写分派；
- 新增事件类型时，只要把它加入本联合，mypy 会在 applier 未处理的分支上报错
  （配合 ``typing.assert_never``），使「漏处理分支」成为类型错误而不是静默丢事件。

不负责：事件的产生、分发、排序与投影。
"""

from typing import Annotated

from pydantic import Field

from app.core.workflows.event.message_event import (
    AssistantPartClosedEvent,
    AssistantTextDeltaEvent,
)
from app.core.workflows.event.run_event import (
    RunInitializedEvent,
    RunStatusChangedEvent,
    UserInputAppendedEvent,
)
from app.core.workflows.event.tool_call_event import (
    ToolCallCreatedEvent,
    ToolCallsSettledEvent,
    ToolCallStatusChangedEvent,
)
from app.core.workflows.event.usage_event import ContextUsageUpdatedEvent, UsageUpdatedEvent

# 按「产生顺序」而非字母序排列：run 建立 → 用户输入 → assistant 输出 → 工具 → 计量，
# 使本清单同时充当一次 run 的时间线说明。
ConversationEvent = Annotated[
    RunInitializedEvent
    | UserInputAppendedEvent
    | RunStatusChangedEvent
    | AssistantTextDeltaEvent
    | AssistantPartClosedEvent
    | ToolCallCreatedEvent
    | ToolCallStatusChangedEvent
    | ToolCallsSettledEvent
    | UsageUpdatedEvent
    | ContextUsageUpdatedEvent,
    Field(discriminator="type"),
]
"""任意一条 conversation 事实；``type`` 字段为判别式。"""
