"""assistant text / reasoning part 事件到 snapshot mutation 的投影。"""

from collections.abc import Sequence

from app.assistant_transport.projection.snapshot_navigation import find_assistant_message, find_run
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_part import ConversationStatePart
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.core.events import AssistantPartClosedEvent, AssistantTextDeltaEvent
from app.models.enums.conversation_run_status import TERMINAL_RUN_STATUSES


def plan_assistant_text_delta(
    event: AssistantTextDeltaEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """追加到同类型的 running part；没有时首次创建该 part。

    异常:
        KeyError: 该 run 的 assistant message 不存在。
    """

    run_index = find_run(state, event.run_id)
    if state["runs"][run_index]["status"] in TERMINAL_RUN_STATUSES:
        return []
    located = find_assistant_message(state, event.run_id)
    assert located is not None
    _, message_index = located
    parts: list[ConversationStatePart] = state["runs"][run_index]["messages"][message_index][
        "parts"
    ]
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
                    ("runs", run_index, "messages", message_index, "parts", part_index, "text"),
                    event.delta,
                )
            ]
    return [
        ConversationStateMutation(
            "set",
            ("runs", run_index, "messages", message_index, "parts", len(parts)),
            {"type": event.part, "text": event.delta, "status": "running"},
        )
    ]


def plan_assistant_part_closed(
    event: AssistantPartClosedEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """把同类型的 running part 标记为 completed；没有 running part 时不变。

    异常:
        KeyError: 该 run 的 assistant message 不存在。
    """

    run_index = find_run(state, event.run_id)
    if state["runs"][run_index]["status"] in TERMINAL_RUN_STATUSES:
        return []
    located = find_assistant_message(state, event.run_id)
    assert located is not None
    _, message_index = located
    parts = state["runs"][run_index]["messages"][message_index]["parts"]
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
                    ("runs", run_index, "messages", message_index, "parts", part_index, "status"),
                    "completed",
                )
            ]
    return []
