"""Canonical conversation state 的中性只读投影类型。

单一职责：定义 ``ConversationStateService`` / ``ConversationRunSubscriptionService`` 输出的
state 快照形状。

职责边界：
- 负责：描述 canonical facts 投影后的 JSON 形状（消息、part、运行状态、revision、错误）。
- 不负责：Assistant Transport 的协议编码、stream 生命周期，以及任何 assistant-ui 语义。

形状定义下沉在本层是为了保证依赖方向恒为 ``api → service``：API 适配层的
``AssistantTransportState`` 等名称只是本模块类型的别名，service 不得反向 import api。

运行时值仍是普通 ``dict`` / ``list``，以便传输层基于 StateProxy 生成增量操作。
"""

from typing import TypedDict

ConversationStatePart = dict[str, object]


class ConversationStateRun(TypedDict):
    """运行元数据的中性投影。"""

    runId: int | None
    status: str


class ConversationStateError(TypedDict):
    """运行错误的稳定结构。"""

    code: str
    message: str
    retryable: bool


class ConversationStateMessage(TypedDict):
    """单条 canonical 消息的中性投影。"""

    id: str
    role: str
    status: str
    endReason: str | None
    createdAt: str
    parts: list[ConversationStatePart]


class ConversationStateSnapshot(TypedDict):
    """canonical conversation state 的完整快照。"""

    messages: list[ConversationStateMessage]
    run: ConversationStateRun
    revision: int
    error: ConversationStateError | None
