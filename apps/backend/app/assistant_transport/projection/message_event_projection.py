"""assistant text / reasoning part 事件到 snapshot mutation 的投影。"""

from collections.abc import Sequence
from typing import cast

from app.assistant_transport.projection.snapshot_navigation import (
    active_run_id,
    ensure_assistant,
    last_assistant_index,
)
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_part import ConversationStatePart
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.core.events import AssistantPartClosedEvent, AssistantTextDeltaEvent


def plan_assistant_text_delta(
    event: AssistantTextDeltaEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """追加到当前活动 Run 助手消息里同类型的 running part；没有时首次创建该 part。

    Run 已终态或不是当前活动时丢弃（迟到 delta）。Run 还没有助手消息（没有用户输入、被结果
    消息唤醒新开的 Run）时连同 part 一起建出助手消息。
    """

    run_id = active_run_id(state, event.run_id)
    if run_id is None:
        return []
    new_part = {"type": event.part, "text": event.delta, "status": "running"}
    message_index, created = ensure_assistant(state, run_id, cast(ConversationStatePart, new_part))
    if message_index is None:
        return created
    parts = state["messages"][message_index]["parts"]
    for part_index in range(len(parts) - 1, -1, -1):
        part = parts[part_index]
        if (
            isinstance(part, dict)
            and part.get("type") == event.part
            and part.get("status") == "running"
        ):
            return [
                ConversationStateMutation(
                    "append-text",
                    ("messages", message_index, "parts", part_index, "text"),
                    event.delta,
                )
            ]
    return [
        ConversationStateMutation(
            "set", ("messages", message_index, "parts", len(parts)), new_part
        )
    ]


def plan_assistant_part_closed(
    event: AssistantPartClosedEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """把同类型的 running part 标记为 completed；没有 running part 或 Run 已终态时不变。"""

    if active_run_id(state, event.run_id) is None:
        return []
    message_index = last_assistant_index(state, event.run_id)
    if message_index is None:
        return []
    parts = state["messages"][message_index]["parts"]
    for part_index in range(len(parts) - 1, -1, -1):
        part = parts[part_index]
        if (
            isinstance(part, dict)
            and part.get("type") == event.part
            and part.get("status") == "running"
        ):
            return [
                ConversationStateMutation(
                    "set",
                    ("messages", message_index, "parts", part_index, "status"),
                    "completed",
                )
            ]
    return []
