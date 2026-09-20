"""Conversation Transport 的单个 Run snapshot 契约。"""

from typing_extensions import TypedDict

from app.assistant_transport.state.conversation_state_error import ConversationStateError
from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_usage import ConversationStateUsage


class ConversationRunSnapshot(TypedDict):
    """一个 Run 的完整 UI 投影。

    ``runId`` 是内部消息、tool part 和 Run usage 的共同归属键；Run 的状态、终态原因、受控
    错误、消息及累计模型 usage 必须在同一对象内保持一致。

    ``error`` 只在 Run 进入失败/取消终态时携带受控文案（``code`` + provider 无关的
    ``message``），供前端展示失败原因；运行中、已结束成功或未发生错误时为 ``None``。它只服务
    UI 渲染与重连恢复，不是 Run 生命周期状态机的第二来源。
    """

    runId: int
    status: str
    endReason: str | None
    messages: list[ConversationStateMessage]
    usage: ConversationStateUsage | None
    error: ConversationStateError | None
