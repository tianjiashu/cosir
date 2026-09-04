"""Conversation 事实事件契约。

本包定义 workflow 内外所有生产者与 applier 之间流通的**中性事实事件**：生产者只描述
「已经发生了什么」，不知道 Transport snapshot 的存在；applier 是唯一把事件翻译成
snapshot mutation 与 canonical fact 的地方。

使用约定：

- 事件描述**已发生的事实**，不是待执行的意图；领域侧完成仲裁与落库后才发出。
- 除 ``*_delta`` 外，事件必须可按业务键（``run_id`` / ``tool_call_id``）重复投递。
- 未知类型的事件由消费者记 warning 后跳过，不得让 run 失败。

模块划分（按事实域，非按产生方）：

- ``conversation_event_envelope``：所有事件共有的信封字段。
- ``run_event``：run 骨架建立、执行状态迁移、用户输入文本。
- ``message_event``：消息内 text / reasoning part 的内容追加与阶段收口。
- ``tool_call_event``：工具调用生命周期与终态批量收束。
- ``usage_event``：token 消耗与上下文占用。
- ``conversation_event``：判别式联合总入口。
"""

from app.core.workflows.event.conversation_event import ConversationEvent
from app.core.workflows.event.conversation_event_envelope import ConversationEventEnvelope
from app.core.workflows.event.message_event import (
    AssistantPartClosedEvent,
    AssistantTextDeltaEvent,
    AssistantTextPartKind,
)
from app.core.workflows.event.run_event import (
    RunInitializedEvent,
    RunStatusChangedEvent,
    UserInputAppendedEvent,
)
from app.core.workflows.event.tool_call_event import (
    ToolCallCreatedEvent,
    ToolCallEventStatus,
    ToolCallsSettledEvent,
    ToolCallStatusChangedEvent,
)
from app.core.workflows.event.usage_event import ContextUsageUpdatedEvent, UsageUpdatedEvent

__all__ = [
    "AssistantPartClosedEvent",
    "AssistantTextDeltaEvent",
    "AssistantTextPartKind",
    "ContextUsageUpdatedEvent",
    "ConversationEvent",
    "ConversationEventEnvelope",
    "RunInitializedEvent",
    "RunStatusChangedEvent",
    "ToolCallCreatedEvent",
    "ToolCallEventStatus",
    "ToolCallStatusChangedEvent",
    "ToolCallsSettledEvent",
    "UsageUpdatedEvent",
    "UserInputAppendedEvent",
]
