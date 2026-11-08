"""事件投影共用的 snapshot 定位与消息骨架辅助（Task 视角，消息扁平排列）。"""

from collections.abc import Iterator
from typing import Literal

from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_mutation import (
    ConversationStateMutation,
    SnapshotPath,
)
from app.assistant_transport.state.conversation_state_part import (
    ConversationStateMessageSource,
    ConversationStatePart,
)
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.assistant_transport.state.message_identity import reply_id
from app.models.enums.conversation_run_status import TERMINAL_RUN_STATUSES


def message(
    message_id: str,
    role: Literal["user", "assistant"],
    parts: list[ConversationStatePart],
    *,
    turn: int,
    inherited: bool = False,
    source: ConversationStateMessageSource | None = None,
) -> ConversationStateMessage:
    """构造消息骨架；结局只在 Run 终态时由 Run 事件投影写入，来源只给 Agent 消息。"""

    return {
        "id": message_id,
        "role": role,
        "parts": parts,
        "turn": turn,
        "inherited": inherited,
        "outcome": None,
        "source": source,
    }


def is_active(state: ConversationStateSnapshot) -> bool:
    """当前活动存在且尚未进入终态：只有这时流式输出与工具预览可以继续投影。

    不按 Run 号比对：Run 事件与模型流事件都在该 Run 仍是当前活动时同步发出（新 Run 只在上一个
    驱动结束后才开出），终态之后到达的输出只可能是同一个 Run 的迟到事件。
    """

    activity = state["activity"]
    return activity is not None and activity["status"] not in TERMINAL_RUN_STATUSES


def reply_index(state: ConversationStateSnapshot, turn: int) -> int | None:
    """回合 ``turn`` 的回复下标；还没有回复时返回 ``None``。"""

    target = reply_id(turn)
    for index in range(len(state["messages"]) - 1, -1, -1):
        if state["messages"][index]["id"] == target:
            return index
    return None


def track_turn(state: ConversationStateSnapshot, turn: int) -> list[ConversationStateMutation]:
    """当前活动的可见回合推进到 ``turn``：结局落点与前端的「当前回复」都读 ``activity.turn``。"""

    activity = state["activity"]
    if activity is None or activity["turn"] == turn:
        return []
    return [ConversationStateMutation("set", ("activity", "turn"), turn)]


def ensure_reply(
    state: ConversationStateSnapshot,
    turn: int,
    first_part: ConversationStatePart,
) -> tuple[int | None, list[ConversationStateMutation]]:
    """定位回合 ``turn`` 的回复；没有时连同首个 part 一起建出来，并推进 ``activity.turn``。

    返回 ``(index, mutations)``：已有回复时 ``index`` 为其下标；新建时 ``index`` 为 ``None``，
    ``mutations`` 已包含带 ``first_part`` 的整条回复（调用方不再追加该 part）。由输入开启的回合在
    并入时已建好回复，走新建分支的只有没有输入开头的回合（唤醒、结果续跑）的第一次输出。
    """

    mutations = track_turn(state, turn)
    index = reply_index(state, turn)
    if index is not None:
        return index, mutations
    mutations.append(
        ConversationStateMutation(
            "set",
            ("messages", len(state["messages"])),
            message(reply_id(turn), "assistant", [first_part], turn=turn),
        )
    )
    return None, mutations


def iter_parts(
    state: ConversationStateSnapshot,
) -> Iterator[tuple[SnapshotPath, ConversationStatePart]]:
    """遍历全部消息 part，产出 ``(part 路径, part)``。"""

    for message_index, current in enumerate(state["messages"]):
        for part_index, part in enumerate(current["parts"]):
            yield ("messages", message_index, "parts", part_index), part


def find_tool(
    state: ConversationStateSnapshot,
    tool_call_id: str,
) -> SnapshotPath | None:
    """按 Task 内唯一 toolCallId 返回 tool-call part 的路径；不存在时返回 ``None``。"""

    for path, part in iter_parts(state):
        if (
            isinstance(part, dict)
            and part.get("type") == "tool-call"
            and part.get("toolCallId") == tool_call_id
        ):
            return path
    return None
