"""工具调用生命周期状态词表（单一事实来源）。

单一职责：作为工具调用从被模型请求到执行结束各生命周期状态的「单一事实来源」Literal 词表。
event 的 ``ToolCallStatusChangedEvent.status`` 与 Transport snapshot 的
``ConversationStateToolCallPart.status`` 都引用此处，避免两处字面量产生拼写漂移。

值即上下文字面量，可直接参与字符串比较与落库；本词表只定义取值集合，不声明状态机迁移规则
（迁移规则由各事件的 ``plan`` 自行校验）。
"""

from typing import Literal

ToolCallEventStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
