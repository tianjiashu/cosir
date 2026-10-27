"""Conversation Transport 的消息契约（Task 视角，扁平排列）。"""

from typing import Literal

from typing_extensions import TypedDict

from app.assistant_transport.state.conversation_state_error import ConversationStateError
from app.assistant_transport.state.conversation_state_part import ConversationStatePart
from app.assistant_transport.state.conversation_state_usage import ConversationStateUsage
from app.core.workflow.inbox.task_message import Delivery


class ConversationStateOutcome(TypedDict):
    """一轮回复的结局：Run 进入终态时写到该 Run 最后一条助手消息上。

    让对话界面不必按 Run 分组也能显示历史回复的「已取消 / 失败 / 用量」；Run 被恢复时清空。
    ``contextWindowTotal`` 是该 Run 冻结的窗口上限，与 ``usage`` 同属一个 Run：上下文占用的
    「已用 / 上限」必须同源，两轮之间换了模型也不会拿新窗口配旧用量。
    """

    status: str
    endReason: str | None
    error: ConversationStateError | None
    usage: ConversationStateUsage | None
    contextWindowTotal: int | None


class ConversationStateMessage(TypedDict):
    """一条 user / assistant 消息。

    ``runId`` 是消息的 Run 归属，只供事件路由、诊断与 fork 定位；对话界面不按它分组。
    ``outcome`` 只有「该 Run 最后一条助手消息」在 Run 终态后非空，user 消息恒为 ``None``。
    ``parts`` 保留各自的流式与工具执行生命周期。
    """

    id: str
    role: Literal["user", "assistant"]
    parts: list[ConversationStatePart]
    runId: int
    outcome: ConversationStateOutcome | None


class ConversationQueuedMessage(TypedDict):
    """尚未被模型边界消费、需投影的排队消息；还没有 Run 归属。

    ``delivery`` 只供前端区分「下个模型边界并入」与「交给下一个 Run」，事实仍在外部队列。
    """

    id: str
    role: Literal["user"]
    parts: list[ConversationStatePart]
    delivery: Delivery
