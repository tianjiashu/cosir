"""Existing tool calls' runtime-only UI updates."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from app.assistant_transport.event.conversation_event_envelope import ConversationEventEnvelope
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_part import ConversationStateToolCallPart
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.config.logging.logger import log
from app.core.tools.schemas.tool_names import TOOL_EXECUTE_TERMINAL


class TerminalOutputDeltaData(BaseModel):
    """One decoded terminal-output chunk for the running tool call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["terminal_output_delta"]
    text: str = Field(min_length=1, max_length=4096)


ToolCallRuntimeUpdateData = Annotated[TerminalOutputDeltaData, Field(discriminator="kind")]


class ToolCallRuntimeUpdateEvent(ConversationEventEnvelope):
    """Project typed runtime-only UI data onto an existing tool-call part.

    These updates only modify the current backend process' transport snapshot. They do not
    create domain facts or persist tool output. ``seq`` is monotonic within one update kind
    and tool call, allowing each projection to reject stale updates independently.
    """

    type: Literal["tool_call_runtime_update"] = "tool_call_runtime_update"
    tool_call_id: str = Field(min_length=1)
    seq: int = Field(ge=0)
    data: ToolCallRuntimeUpdateData

    def plan(self, state: ConversationStateSnapshot) -> Sequence[ConversationStateMutation]:
        """Project the selected runtime payload only to its compatible tool part."""

        try:
            run_index = self._find_run(state, self.run_id)
        except KeyError:
            log.warning(
                "tool_runtime_update_run_missing",
                extra={
                    "msg": "运行期工具投影目标 Run 不存在，跳过事件",
                    "data": {"task_id": self.task_id, "run_id": self.run_id},
                },
            )
            return []

        run = state["runs"][run_index]
        if isinstance(self.data, TerminalOutputDeltaData) and run["status"] != "running":
            return []

        located: tuple[int, int] | None = None
        for message_index, message in enumerate(run["messages"]):
            for part_index, part in enumerate(message["parts"]):
                if (
                    isinstance(part, dict)
                    and part.get("type") == "tool-call"
                    and part.get("toolCallId") == self.tool_call_id
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
                        "task_id": self.task_id,
                        "run_id": self.run_id,
                        "tool_call_id": self.tool_call_id,
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
        return self._plan_terminal_output(part, base)

    def _plan_terminal_output(
        self,
        part: ConversationStateToolCallPart,
        base: tuple[str | int, ...],
    ) -> Sequence[ConversationStateMutation]:
        """Append an output chunk only while its execute_terminal part is running."""
        data = self.data
        if (
            not isinstance(data, TerminalOutputDeltaData)
            or part.get("toolName") != TOOL_EXECUTE_TERMINAL
            or part.get("status") != "running"
        ):
            return []
        previous_seq = part.get("terminal_output_seq")
        if isinstance(previous_seq, int) and previous_seq >= self.seq:
            return []

        display_data = part.get("display_data")
        if display_data is None:
            mutations: list[ConversationStateMutation] = [
                ConversationStateMutation(
                    "set",
                    (*base, "display_data"),
                    {
                        "kind": "terminal-result",
                        "output": data.text,
                    },
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
        mutations.append(ConversationStateMutation("set", (*base, "terminal_output_seq"), self.seq))
        return mutations


__all__ = [
    "TerminalOutputDeltaData",
    "ToolCallRuntimeUpdateData",
    "ToolCallRuntimeUpdateEvent",
]
