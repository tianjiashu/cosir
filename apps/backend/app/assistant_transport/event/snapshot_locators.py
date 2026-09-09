"""Snapshot 定位与消息骨架构造辅助。

本模块只承载「在 ``ConversationStateSnapshot`` 中按业务键定位 message / part / 工具，以及
构造 user / assistant 消息骨架」这一单一职责，是各事件 ``plan`` 方法的纯函数依赖。所有函数
无状态、不接触 storage / 网络，且只读取 ``state`` 不修改它。

不负责：事件的分发、mutation 之外的 snapshot 写语义（由各事件 ``plan`` 决定）。
"""

from typing import Literal

from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_part import ConversationStatePart
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot


def _message(
    message_id: str,
    run_id: int | None,
    role: Literal["user", "assistant"],
    status: str,
    parts: list[ConversationStatePart],
) -> ConversationStateMessage:
    """构造 Transport user/assistant 消息骨架。

    assistant 消息不预置空 text part，避免模型先输出 reasoning 时把真实 part 顺序错误地
    固定为 ``text -> reasoning``。

    参数:
        message_id: 消息标识（``user-<run_id>`` / ``assistant-<run_id>``）。
        run_id: 所属 Conversation Run 标识。
        role: 消息角色（``user`` / ``assistant``）。
        status: 消息初始状态。
        parts: 消息初始 part 列表。

    返回:
        符合 ``ConversationStateMessage`` 契约的消息骨架字典。

    异常:
        无。

    副作用:
        无；返回新字典，不修改入参。
    """

    return {
        "id": message_id,
        "runId": run_id,
        "role": role,
        "status": status,
        "endReason": None,
        "parts": parts,
    }


def _find_assistant_message(
    state: ConversationStateSnapshot,
    run_id: int | None,
    *,
    required: bool = True,
) -> int | None:
    """按 run 找到 assistant message 的下标。

    参数:
        state: 当前 Task snapshot。
        run_id: 目标 Run 标识。
        required: 未找到时是否抛出 ``KeyError``；``False`` 时返回 ``None``。

    返回:
        assistant message 在 ``state["messages"]`` 中的下标；``required=False`` 且未找到时
        返回 ``None``。

    异常:
        KeyError: ``required=True`` 且对应 assistant message 不存在。

    副作用:
        无。
    """

    for index, message in enumerate(state["messages"]):
        if message.get("runId") == run_id and message.get("role") == "assistant":
            return index
    if required:
        raise KeyError(f"assistant message for run {run_id} not found")
    return None


def _find_message_part(
    state: ConversationStateSnapshot,
    run_id: int | None,
    role: str,
    part_type: str,
) -> tuple[int, int]:
    """按 run、role 与 part 类型定位消息 part。

    参数:
        state: 当前 Task snapshot。
        run_id: 目标 Run 标识。
        role: 目标消息角色。
        part_type: 目标 part 类型（``"text"`` / ``"reasoning"``）。

    返回:
        ``(message 下标, part 下标)`` 元组。

    异常:
        KeyError: 对应 part 不存在。

    副作用:
        无。
    """

    for message_index, message in enumerate(state["messages"]):
        if message.get("runId") != run_id or message.get("role") != role:
            continue
        for part_index, part in enumerate(message["parts"]):
            if isinstance(part, dict) and part.get("type") == part_type:
                return message_index, part_index
    raise KeyError(f"{role} {part_type} part for run {run_id} not found")


def _find_tool(state: ConversationStateSnapshot, tool_call_id: str) -> tuple[int, int]:
    """按 Task 内唯一 toolCallId 定位 tool part。

    参数:
        state: 当前 Task snapshot。
        tool_call_id: 工具调用标识。

    返回:
        ``(message 下标, part 下标)`` 元组。

    异常:
        KeyError: 对应 tool-call part 不存在。

    副作用:
        无。
    """

    for message_index, message in enumerate(state["messages"]):
        for part_index, part in enumerate(message["parts"]):
            if (
                isinstance(part, dict)
                and part.get("type") == "tool-call"
                and part.get("toolCallId") == tool_call_id
            ):
                return message_index, part_index
    raise KeyError(tool_call_id)
