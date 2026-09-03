"""Assistant Transport 使用的中性 ConversationState JSON 契约。"""

from typing import NotRequired, TypedDict

from app.assistant_transport.state.conversation_state_part import ConversationStatePart


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
    runId: NotRequired[int | None]
    role: str
    status: str
    endReason: str | None
    createdAt: str
    parts: list[ConversationStatePart]


class ConversationStateSnapshot(TypedDict):
    """一个 Task 的完整 Transport state 快照。"""

    messages: list[ConversationStateMessage]
    run: ConversationStateRun
    error: ConversationStateError | None
