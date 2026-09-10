"""Conversation event 的共有信封、投影契约与 Run 内 snapshot 导航辅助。"""

from abc import abstractmethod
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_part import ConversationStatePart
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot


class ConversationEventEnvelope(BaseModel):
    """所有 conversation event 的共有信封与 snapshot 投影契约。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: int = Field(ge=1)
    # ContextUsageUpdatedEvent 不携带 run_id；其余 Run 内事件必须携带。
    run_id: int | None = None
    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    step_id: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @abstractmethod
    def plan(
        self,
        state: ConversationStateSnapshot,
    ) -> Sequence[ConversationStateMutation]:
        """把已发生的事实投影为 snapshot mutations。"""
        raise NotImplementedError

    @staticmethod
    def _message(
        message_id: str,
        role: Literal["user", "assistant"],
        parts: list[ConversationStatePart],
    ) -> ConversationStateMessage:
        """构造不重复保存 Run 字段的消息骨架。"""

        return {"id": message_id, "role": role, "parts": parts}

    @staticmethod
    def _find_run(state: ConversationStateSnapshot, run_id: int | None) -> int:
        if run_id is None:
            raise KeyError("run id is required")
        for index, run in enumerate(state["runs"]):
            if run["runId"] == run_id:
                return index
        raise KeyError(f"run {run_id} not found")

    @staticmethod
    def _find_assistant_message(
        state: ConversationStateSnapshot,
        run_id: int | None,
        *,
        required: bool = True,
    ) -> tuple[int, int] | None:
        """返回 ``(run_index, message_index)``。"""

        run_index = ConversationEventEnvelope._find_run(state, run_id)
        for message_index, message in enumerate(state["runs"][run_index]["messages"]):
            if message["role"] == "assistant":
                return run_index, message_index
        if required:
            raise KeyError(f"assistant message for run {run_id} not found")
        return None

    @staticmethod
    def _find_message_part(
        state: ConversationStateSnapshot,
        run_id: int | None,
        role: str,
        part_type: str,
    ) -> tuple[int, int, int]:
        """返回 ``(run_index, message_index, part_index)``。"""

        run_index = ConversationEventEnvelope._find_run(state, run_id)
        for message_index, message in enumerate(state["runs"][run_index]["messages"]):
            if message["role"] != role:
                continue
            for part_index, part in enumerate(message["parts"]):
                if isinstance(part, dict) and part.get("type") == part_type:
                    return run_index, message_index, part_index
        raise KeyError(f"{role} {part_type} part for run {run_id} not found")

    @staticmethod
    def _find_tool(
        state: ConversationStateSnapshot,
        tool_call_id: str,
    ) -> tuple[int, int, int]:
        """按 Task 内唯一 toolCallId 返回 ``(run_index, message_index, part_index)``。"""

        for run_index, run in enumerate(state["runs"]):
            for message_index, message in enumerate(run["messages"]):
                for part_index, part in enumerate(message["parts"]):
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "tool-call"
                        and part.get("toolCallId") == tool_call_id
                    ):
                        return run_index, message_index, part_index
        raise KeyError(tool_call_id)
