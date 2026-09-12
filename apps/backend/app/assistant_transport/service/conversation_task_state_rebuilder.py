"""Pure reconstruction of Assistant Transport state from canonical conversation facts."""

from __future__ import annotations

from collections.abc import Sequence
from itertools import groupby
from operator import attrgetter
from typing import cast

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages.tool import ToolCall

from app.assistant_transport.state.conversation_run_snapshot import ConversationRunSnapshot
from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_part import (
    ConversationStateTextPart,
    ConversationStateToolCallPart,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
)
from app.config.configuration import get_tool_registry
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.task_record import TaskRecord


class ConversationTaskStateRebuilder:
    """Rebuild a Task Transport snapshot from Task, Run, and context records only."""

    @staticmethod
    def build_pair_tool_part(
        rows: list[ConversationTaskContextRecord],
    ) -> dict[str, ConversationStateToolCallPart]:
        tool_parts: dict[str, ConversationStateToolCallPart] = {}
        for row in rows:
            message: BaseMessage = row.message
            if isinstance(message, AIMessage) and cast(AIMessage, message).tool_calls:
                ai_message = cast(AIMessage, message)
                calls: list[ToolCall] = ai_message.tool_calls
                for call in calls:
                    tool_parts[call.get("id")] = ConversationStateToolCallPart(
                        type="tool-call",
                        toolCallId=call.get("id"),
                        toolName=call.get("name"),
                        args=call.get("args"),
                        presentation=ConversationTaskStateRebuilder.get_tool_display(call.get("name")),
                        status="cancelled",
                    )
            if isinstance(message, ToolMessage):
                tool_message = cast(ToolMessage, message)
                tool_part: ConversationStateToolCallPart = tool_parts.get(tool_message.tool_call_id)
                if tool_part is None:
                    raise RuntimeError("未闭合tool")
                tool_part["status"] = row.transport_metadata.get("status")
                tool_part["display_data"] = row.transport_metadata.get("display_data")
                if tool_part["status"] == "failed":
                    tool_part["isError"] = True
                    tool_part["error"] = row.transport_metadata.get("error")
                else:
                    tool_part["isError"] = False
                    tool_part["error"] = None

        return tool_parts

    @staticmethod
    def get_tool_display(tool_name: str) -> dict[str, object] | None:
        if not tool_name:
            return None
        tool_registry = get_tool_registry()
        tool_definition = tool_registry.get_tool_definition(tool_name)
        if not tool_definition or not tool_definition.display:
            return None
        return tool_definition.display.to_dict()

    @staticmethod
    def rebuild(
            task: TaskRecord,
            runs: Sequence[ConversationRunRecord],
            context_rows: Sequence[ConversationTaskContextRecord],
    ) -> ConversationStateSnapshot:
        """Return a validated snapshot assembled from the three canonical record types.

        Parameters:
            task: Task-level current run and context usage facts.
            runs: All Run records belonging to ``task``.
            context_rows: Persisted LangChain messages and Transport metadata for the Task.

        Returns:
            A new ``ConversationStateSnapshot``. Run ordering is ``created_at`` then id;
            assistant rows for one Run are merged and all message ids come from context row ids.

        Raises:
            RuntimeError: If a tool result row has no matching AI tool call in the same Run.

        Side effects:
            None. This method does not access sessions, snapshot storage, checkpoints, tools,
            frontend runtime state, or the in-memory Transport registry.
        """

        run_records = list(runs)
        run_groups = {
            run_id: list(rows)
            for run_id, rows in groupby(
                sorted(context_rows, key=attrgetter("run_id")),
                key=attrgetter("run_id"),
            )
        }

        snapshot_runs: list[ConversationRunSnapshot] = []

        for run in run_records:
            # Run 没有 context 行也必须出现在快照里：`current_run_id` 必须指向快照中的某个
            # Run（``validate_snapshot`` 强校验），而新建 Run 在写入首条消息前正是这个状态。
            rows: list[ConversationTaskContextRecord] = sorted(
                run_groups.get(run.id, []), key=lambda row: row.sequence
            )

            tool_parts_dict = ConversationTaskStateRebuilder.build_pair_tool_part(rows)

            snapshot_messages: list[ConversationStateMessage] = []
            assistant_message = ConversationStateMessage(
                id=f"assistant-{run.id}", role="assistant", parts=[]
            )
            for row in rows:
                message = row.message
                if isinstance(message, HumanMessage):
                    snapshot_messages.append(ConversationStateMessage(
                        id=f"user-{run.id}",
                        role="user",
                        parts=[ConversationStateTextPart(
                            type="text",
                            text=message.content,
                            status="completed"
                        )]))
                elif isinstance(message, AIMessage):
                    ai_message: AIMessage = cast(AIMessage, message)
                    if ai_message.additional_kwargs.get("reasoning_content") is not None:
                        assistant_message["parts"].append(
                            ConversationStateTextPart(
                                type="reasoning",
                                text=ai_message.additional_kwargs["reasoning_content"],
                                status="completed"
                            )
                        )
                    if ai_message.content is not None:
                        assistant_message["parts"].append(
                            ConversationStateTextPart(
                                type="text",
                                text=ai_message.content,
                                status="completed"
                            )
                        )
                    if ai_message.tool_calls is not None and len(ai_message.tool_calls) > 0:
                        calls: list[ToolCall] = ai_message.tool_calls
                        for call in calls:
                            assistant_message["parts"].append(
                                tool_parts_dict[call.get("id")]
                            )
            snapshot_messages.append(assistant_message)
            snapshot_runs.append(ConversationRunSnapshot(
                runId=run.id,
                status=run.status,
                endReason=run.end_reason,
                messages=snapshot_messages,
                usage=run.usage
            ))
        used = task.context_usage_used
        total = task.context_window_total
        ratio = None if used is None or total is None or total == 0 else used / total
        return ConversationStateSnapshot(
            runs=snapshot_runs,
            current_run_id=task.current_run_id,
            context_usage_used=used,
            context_window_total=total,
            error=None,
            context_usage_ratio=ratio,
            approvals={}
        )


__all__ = [
    "ConversationTaskStateRebuilder",
]
