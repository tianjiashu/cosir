"""工具调用及其生命周期的 UI part 契约。"""

from typing import Literal, NotRequired, TypedDict

ToolCallStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


class ConversationStateToolCallPart(TypedDict):
    """工具调用及其生命周期的 UI part。

    描述一次工具调用从待执行到完成/失败/取消的完整生命周期，并携带参数、结果
    与可能的错误信息。status 取值受限为 ToolCallStatus 的五个枚举。
    """

    type: Literal["tool-call"]
    toolCallId: str
    toolName: str
    status: ToolCallStatus
    args: NotRequired[dict[str, object]]
    result: NotRequired[object]
    error: NotRequired[object]
    isError: NotRequired[bool]
    approvalRequestId: NotRequired[None]
