"""Runtime-only tool locator events used by Workbench surfaces."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from app.assistant_transport.event.conversation_event_envelope import ConversationEventEnvelope
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_part import ConversationStateToolCallPart
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.config.logging.logger import log


class DelegationRefData(BaseModel):
    """Strict payload for the v1 delegation locator event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    child_task_id: int = Field(ge=1)
    title: str = Field(min_length=1, pattern=r".*\S.*")
    role: str = Field(min_length=1, pattern=r".*\S.*")


class ToolCallRuntimeUpdateEvent(ConversationEventEnvelope):
    """Attach a runtime-only locator to an existing parent tool part.

    ``tool_call_id`` is used only to target the in-memory parent snapshot. The event does not
    create a delegation record and does not persist the tool locator as a new domain fact.
    """

    type: Literal["tool_call_runtime_update"] = "tool_call_runtime_update"
    tool_call_id: str = Field(min_length=1)
    kind: Literal["delegation_ref"]
    seq: int = Field(ge=0)
    data: DelegationRefData

    def plan(self, state: ConversationStateSnapshot) -> Sequence[ConversationStateMutation]:
        """Project the delegation locator only into the targeted parent tool part."""

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
        located: tuple[int, int, int] | None = None
        for message_index, message in enumerate(state["runs"][run_index]["messages"]):
            for part_index, part in enumerate(message["parts"]):
                if (
                    isinstance(part, dict)
                    and part.get("type") == "tool-call"
                    and part.get("toolCallId") == self.tool_call_id
                ):
                    located = run_index, message_index, part_index
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

        _, _, part_index = located
        part = cast(
            ConversationStateToolCallPart,
            state["runs"][run_index]["messages"][located[1]]["parts"][part_index],
        )
        if part.get("toolName") != "delegate_task":
            return []
        old_child_task_id = part.get("child_task_id")
        old_seq = part.get("delegation_ref_seq")
        if isinstance(old_seq, int) and old_seq >= self.seq:
            return []
        if old_child_task_id is not None:
            if old_child_task_id != self.data.child_task_id:
                return []
            if part.get("agent_role") == self.data.role:
                return []
        base = ("runs", run_index, "messages", located[1], "parts", part_index)
        mutations = [
            ConversationStateMutation("set", (*base, "child_task_id"), self.data.child_task_id),
            ConversationStateMutation("set", (*base, "agent_role"), self.data.role),
            ConversationStateMutation("set", (*base, "delegation_ref_seq"), self.seq),
            ConversationStateMutation(
                "set",
                (*base, "display_data"),
                {
                    "kind": "delegation-result",
                    "title": self.data.title,
                    "role": self.data.role,
                    "child_task_id": self.data.child_task_id,
                },
            ),
        ]
        return mutations


__all__ = ["DelegationRefData", "ToolCallRuntimeUpdateEvent"]
