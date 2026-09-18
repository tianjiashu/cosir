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
    display_data: NotRequired[dict[str, object] | None]
    isError: NotRequired[bool]
    approvalRequestId: NotRequired[None]
    # Runtime delegation locator. It is not rendered as text by the frontend.
    child_task_id: NotRequired[int]
    agent_role: NotRequired[str]
    delegation_ref_seq: NotRequired[int]
    # Runtime terminal output sequence; it guards against stale delta events.
    terminal_output_seq: NotRequired[int]
