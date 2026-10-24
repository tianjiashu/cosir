"""事件投影共用的 snapshot 定位与消息骨架辅助。"""

from typing import Literal

from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_part import ConversationStatePart
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot


def message(
    message_id: str,
    role: Literal["user", "assistant"],
    parts: list[ConversationStatePart],
) -> ConversationStateMessage:
    """构造不重复保存 Run 字段的消息骨架。"""

    return {"id": message_id, "role": role, "parts": parts}


def find_run(state: ConversationStateSnapshot, run_id: int | None) -> int:
    if run_id is None:
        raise KeyError("run id is required")
    for index, run in enumerate(state["runs"]):
        if run["runId"] == run_id:
            return index
    raise KeyError(f"run {run_id} not found")


def find_assistant_message(
    state: ConversationStateSnapshot,
    run_id: int | None,
    *,
    required: bool = True,
) -> tuple[int, int] | None:
    """返回 ``(run_index, message_index)``。"""

    run_index = find_run(state, run_id)
    for message_index in range(len(state["runs"][run_index]["messages"]) - 1, -1, -1):
        if state["runs"][run_index]["messages"][message_index]["role"] == "assistant":
            return run_index, message_index
    if required:
        raise KeyError(f"assistant message for run {run_id} not found")
    return None


def find_tool(
    state: ConversationStateSnapshot,
    tool_call_id: str,
    *,
    required: bool = True,
) -> tuple[int, int, int] | None:
    """按 Task 内唯一 toolCallId 返回 ``(run_index, message_index, part_index)``。

    参数:
        state: 当前 Task snapshot。
        tool_call_id: 目标工具调用标识。
        required: 找不到 part 时是否视为契约错误。``True`` 时抛 ``KeyError``；
            ``False`` 时返回 ``None``，供调用方把「展示事实缺失」降级为可跳过状态。

    异常:
        KeyError: ``required=True`` 且整个 snapshot 内不存在该 toolCallId 的 part。
    """

    for run_index, run in enumerate(state["runs"]):
        for message_index, run_message in enumerate(run["messages"]):
            for part_index, part in enumerate(run_message["parts"]):
                if (
                    isinstance(part, dict)
                    and part.get("type") == "tool-call"
                    and part.get("toolCallId") == tool_call_id
                ):
                    return run_index, message_index, part_index
    if required:
        raise KeyError(tool_call_id)
    return None
