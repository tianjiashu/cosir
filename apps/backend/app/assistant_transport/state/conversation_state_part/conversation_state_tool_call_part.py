"""工具调用及其生命周期的 UI part 契约。"""

from typing import Literal, NotRequired

from typing_extensions import TypedDict

from app.models.enums.tool_call_status import ToolCallEventStatus

# 与事件侧共用同一事实来源，避免两处字面量漂移。
ToolCallStatus = ToolCallEventStatus


class ConversationStateToolCallPart(TypedDict):
    """工具调用及其生命周期的 UI part。

    描述一次工具调用从待执行到完成/失败/取消的完整生命周期，并携带参数
    与可能的错误信息。status 取值受限为 ToolCallEventStatus（见
    ``app.models.enums.tool_call_status``）的五个枚举。
    """

    type: Literal["tool-call"]
    toolCallId: str
    toolName: str
    status: ToolCallStatus
    args: NotRequired[dict[str, object]]
    error: NotRequired[str | None]
    presentation: NotRequired[dict[str, object]]
    data: NotRequired[dict[str, object] | None]
    isError: NotRequired[bool]
    approvalRequestId: NotRequired[None]
