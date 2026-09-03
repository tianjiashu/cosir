"""ConversationState 工具调用 part 的中性 JSON 契约。"""

from typing import Literal, NotRequired, TypedDict


class ConversationStateToolError(TypedDict):
    """工具调用失败的结构化错误。"""

    code: str
    message: str
    retryable: bool
    details: NotRequired[object]


class ConversationStateToolApproval(TypedDict):
    """工具调用等待人工审批时的中性审批摘要。"""

    requestId: str
    risk: str
    summary: str


class ConversationStateToolCallPart(TypedDict):
    """工具调用消息 part 的中性投影。"""

    type: Literal["tool-call"]
    toolCallId: str
    toolName: str
    status: Literal[
        "pending",
        "running",
        "requires-action",
        "completed",
        "failed",
        "cancelled",
    ]
    args: NotRequired[dict[str, object]]
    argsText: NotRequired[str]
    result: NotRequired[object]
    error: NotRequired[str | ConversationStateToolError]
    approval: NotRequired[ConversationStateToolApproval]
    createdAt: NotRequired[str]
    updatedAt: NotRequired[str]
    isError: NotRequired[bool]
