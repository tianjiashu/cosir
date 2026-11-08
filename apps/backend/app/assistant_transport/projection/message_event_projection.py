"""assistant text part 事件到 snapshot mutation 的投影。"""

from collections.abc import Sequence
from typing import cast

from app.assistant_transport.projection.snapshot_navigation import (
    ensure_reply,
    is_active,
    reply_index,
)
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_part import ConversationStatePart
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.core.events import AssistantPartClosedEvent, AssistantTextDeltaEvent


def _running_text_index(
    state: ConversationStateSnapshot, message_index: int, source_sequence: int
) -> int | None:
    parts = state["messages"][message_index]["parts"]
    for part_index in range(len(parts) - 1, -1, -1):
        part = parts[part_index]
        if (
            isinstance(part, dict)
            and part.get("type") == "text"
            and part.get("status") == "running"
            and part.get("sourceSequence") == source_sequence
        ):
            return part_index
    return None


def plan_assistant_text_delta(
    event: AssistantTextDeltaEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """追加到这一回合回复里同一响应的 running 正文 part；没有时首次创建该 part。

    当前活动已终态时丢弃（迟到 delta）。这一回合还没有回复（没有输入开头的回合第一次输出）时连同
    part 一起建出回复。
    """

    if not is_active(state):
        return []
    new_part = {
        "type": event.part,
        "text": event.delta,
        "status": "running",
        "sourceSequence": event.source_sequence,
    }
    message_index, mutations = ensure_reply(
        state, event.turn, cast(ConversationStatePart, new_part)
    )
    if message_index is None:
        return mutations
    part_index = _running_text_index(state, message_index, event.source_sequence)
    if part_index is not None:
        return [
            *mutations,
            ConversationStateMutation(
                "append-text",
                ("messages", message_index, "parts", part_index, "text"),
                event.delta,
            ),
        ]
    parts = state["messages"][message_index]["parts"]
    return [
        *mutations,
        ConversationStateMutation(
            "set", ("messages", message_index, "parts", len(parts)), new_part
        ),
    ]


def plan_assistant_part_closed(
    event: AssistantPartClosedEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """把同一响应的 running 正文 part 标记为 completed；没有 running part 或已终态时不变。"""

    if not is_active(state):
        return []
    message_index = reply_index(state, event.turn)
    if message_index is None:
        return []
    part_index = _running_text_index(state, message_index, event.source_sequence)
    if part_index is None:
        return []
    return [
        ConversationStateMutation(
            "set",
            ("messages", message_index, "parts", part_index, "status"),
            "completed",
        )
    ]
