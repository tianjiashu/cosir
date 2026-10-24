"""运行中工具调用的运行期更新到 snapshot mutation 的投影。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from app.assistant_transport.projection.snapshot_navigation import find_run
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_part import ConversationStateToolCallPart
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.config.logging.logger import log
from app.core.events import TerminalOutputDeltaData, ToolCallRuntimeUpdateEvent
from app.core.tools.schemas.tool_names import TOOL_EXECUTE_TERMINAL


def plan_tool_call_runtime_update(
    event: ToolCallRuntimeUpdateEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """只把运行期数据投影到兼容的 tool-call part；目标缺失时跳过并留痕。"""

    try:
        run_index = find_run(state, event.run_id)
    except KeyError:
        log.warning(
            "tool_runtime_update_run_missing",
            extra={
                "msg": "运行期工具投影目标 Run 不存在，跳过事件",
                "data": {"task_id": event.task_id, "run_id": event.run_id},
            },
        )
        return []

    run = state["runs"][run_index]
    if isinstance(event.data, TerminalOutputDeltaData) and run["status"] != "running":
        return []

    located: tuple[int, int] | None = None
    for message_index, run_message in enumerate(run["messages"]):
        for part_index, part in enumerate(run_message["parts"]):
            if (
                isinstance(part, dict)
                and part.get("type") == "tool-call"
                and part.get("toolCallId") == event.tool_call_id
            ):
                located = message_index, part_index
                break
        if located is not None:
            break
    if located is None:
        log.warning(
            "tool_runtime_update_part_missing",
            extra={
                "msg": "运行期工具投影目标不存在，跳过事件",
                "data": {
                    "task_id": event.task_id,
                    "run_id": event.run_id,
                    "tool_call_id": event.tool_call_id,
                },
            },
        )
        return []

    message_index, part_index = located
    part = cast(
        ConversationStateToolCallPart,
        run["messages"][message_index]["parts"][part_index],
    )
    base = ("runs", run_index, "messages", message_index, "parts", part_index)
    return _plan_terminal_output(event, part, base)


def _plan_terminal_output(
    event: ToolCallRuntimeUpdateEvent,
    part: ConversationStateToolCallPart,
    base: tuple[str | int, ...],
) -> Sequence[ConversationStateMutation]:
    """Append an output chunk only while its execute_terminal part is running."""

    data = event.data
    if (
        not isinstance(data, TerminalOutputDeltaData)
        or part.get("toolName") != TOOL_EXECUTE_TERMINAL
        or part.get("status") != "running"
    ):
        return []
    previous_seq = part.get("terminal_output_seq")
    if isinstance(previous_seq, int) and previous_seq >= event.seq:
        return []

    display_data = part.get("display_data")
    if display_data is None:
        mutations: list[ConversationStateMutation] = [
            ConversationStateMutation(
                "set",
                (*base, "display_data"),
                {"kind": "terminal-result", "output": data.text},
            )
        ]
    elif display_data.get("kind") != "terminal-result":
        return []
    else:
        mutations = []
        current_output = display_data.get("output")
        if not isinstance(current_output, str):
            mutations.append(
                ConversationStateMutation("set", (*base, "display_data", "output"), data.text)
            )
        elif data.text:
            mutations.append(
                ConversationStateMutation(
                    "append-text", (*base, "display_data", "output"), data.text
                )
            )
    mutations.append(ConversationStateMutation("set", (*base, "terminal_output_seq"), event.seq))
    return mutations
