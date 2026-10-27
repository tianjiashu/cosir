"""事件投影共用的 snapshot 定位与消息骨架辅助（Task 视角，消息扁平排列）。"""

from collections.abc import Iterator
from typing import Literal

from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_mutation import (
    ConversationStateMutation,
    SnapshotPath,
)
from app.assistant_transport.state.conversation_state_part import ConversationStatePart
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.assistant_transport.state.message_identity import leading_reply_id
from app.models.enums.conversation_run_status import TERMINAL_RUN_STATUSES


def message(
    message_id: str,
    role: Literal["user", "assistant"],
    parts: list[ConversationStatePart],
    run_id: int,
) -> ConversationStateMessage:
    """构造消息骨架；结局只在 Run 终态时由 Run 事件投影写入。"""

    return {"id": message_id, "role": role, "parts": parts, "runId": run_id, "outcome": None}


def is_current_run(state: ConversationStateSnapshot, run_id: int | None) -> bool:
    activity = state["activity"]
    return activity is not None and run_id is not None and activity["runId"] == run_id


def active_run_id(state: ConversationStateSnapshot, run_id: int | None) -> int | None:
    """事件所属 Run 是当前活动且尚未进入终态时返回它的 id，否则 ``None``：只有这个 Run 的流式
    输出与工具预览可以继续投影。"""

    activity = state["activity"]
    if (
        activity is None
        or activity["runId"] != run_id
        or activity["status"] in TERMINAL_RUN_STATUSES
    ):
        return None
    return run_id


def last_assistant_index(state: ConversationStateSnapshot, run_id: int | None) -> int | None:
    """该 Run 最后一条助手消息的下标；Run 还没有助手消息时返回 ``None``。"""

    for index in range(len(state["messages"]) - 1, -1, -1):
        current = state["messages"][index]
        if current["runId"] == run_id and current["role"] == "assistant":
            return index
    return None


def ensure_assistant(
    state: ConversationStateSnapshot,
    run_id: int,
    first_part: ConversationStatePart,
) -> tuple[int | None, list[ConversationStateMutation]]:
    """定位当前 Run 的助手消息；没有时连同首个 part 一起建出来。

    返回 ``(index, mutations)``：已有助手消息时 ``index`` 为其下标、``mutations`` 为空；新建时
    ``index`` 为 ``None``，``mutations`` 已包含带 ``first_part`` 的整条消息（调用方不再追加该
    part）。没有用户输入的 Run（被结果消息唤醒新开）第一次输出时走新建分支。
    """

    index = last_assistant_index(state, run_id)
    if index is not None:
        return index, []
    return None, [
        ConversationStateMutation(
            "set",
            ("messages", len(state["messages"])),
            message(leading_reply_id(run_id), "assistant", [first_part], run_id),
        )
    ]


def iter_parts(
    state: ConversationStateSnapshot, run_id: int | None = None
) -> Iterator[tuple[SnapshotPath, ConversationStatePart]]:
    """遍历全部（或某 Run 的）消息 part，产出 ``(part 路径, part)``。"""

    for message_index, current in enumerate(state["messages"]):
        if run_id is not None and current["runId"] != run_id:
            continue
        for part_index, part in enumerate(current["parts"]):
            yield ("messages", message_index, "parts", part_index), part


def find_tool(
    state: ConversationStateSnapshot,
    tool_call_id: str,
    run_id: int | None = None,
) -> SnapshotPath | None:
    """按 Task 内唯一 toolCallId 返回 tool-call part 的路径；不存在时返回 ``None``。"""

    for path, part in iter_parts(state, run_id):
        if (
            isinstance(part, dict)
            and part.get("type") == "tool-call"
            and part.get("toolCallId") == tool_call_id
        ):
            return path
    return None
