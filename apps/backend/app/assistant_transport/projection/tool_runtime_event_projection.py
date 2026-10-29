"""运行中工具调用的运行期更新到 snapshot mutation 的投影。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from app.assistant_transport.projection.snapshot_navigation import find_tool
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_part import ConversationStateToolCallPart
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.config.logging.logger import log
from app.core.events import TerminalOutputDeltaData, ToolCallRuntimeUpdateEvent
from app.core.tool.schemas.tool_names import TOOL_EXECUTE_TERMINAL


def plan_tool_call_runtime_update(
    event: ToolCallRuntimeUpdateEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """只把运行期数据投影到兼容的 tool-call part；目标缺失时跳过并留痕。

    是否还接收输出只看 part 自己的状态（见 :func:`_plan_terminal_output`），不看所属 Run：转到后台的
    命令在 Run 挂起、结束甚至开了新 Run 之后仍在输出，Run 状态与命令是否还活着无关。
    """

    base = find_tool(state, event.tool_call_id, event.run_id)
    if base is None:
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
    _, message_index, _, part_index = base
    part = cast(
        ConversationStateToolCallPart,
        state["messages"][cast(int, message_index)]["parts"][cast(int, part_index)],
    )
    return _plan_terminal_output(event, part, base)


def _plan_terminal_output(
    event: ToolCallRuntimeUpdateEvent,
    part: ConversationStateToolCallPart,
    base: tuple[str | int, ...],
) -> Sequence[ConversationStateMutation]:
    """命令还活着时追加输出块：前台调用是 ``running``，转到后台的是带 ``deferred`` 的 ``pending`` 占位。

    命令结束、取消或被撤销时 part 都会离开这两种状态，迟到的块因此自然丢弃；``seq`` 另外挡住重复帧。
    """

    data = event.data
    status = part.get("status")
    if (
        not isinstance(data, TerminalOutputDeltaData)
        or part.get("toolName") != TOOL_EXECUTE_TERMINAL
        or not (status == "running" or (status == "pending" and part.get("deferred") is True))
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
