"""Pure reconstruction of Assistant Transport state from canonical conversation facts."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any, cast

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.assistant_transport.state.conversation_run_snapshot import ConversationRunSnapshot
from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_part import ConversationStatePart
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    validate_snapshot,
)
from app.core.context.agent_context_loader import AgentContextLoader, load_agent_context
from app.core.tools.tool_execute.tool_error import normalize_status_hint
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.json_helpers import validate_transport_metadata
from app.models.task_record import TaskRecord
from app.utils.message_content import content_to_text

_SUPPORTED_MESSAGE_SCHEMA_VERSION = 1
_SUPPORTED_TRANSPORT_SCHEMA_VERSION = 1
_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}


class ConversationStateRebuildError(ValueError):
    """Structured failure raised when canonical facts cannot form a valid snapshot."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        task_id: int,
        run_id: int | None = None,
        context_row_id: int | None = None,
    ) -> None:
        self.code = code
        self.task_id = task_id
        self.run_id = run_id
        self.context_row_id = context_row_id
        super().__init__(message)

    def as_dict(self) -> dict[str, object]:
        """Return safe structured diagnostics without including message content or metadata."""

        return {
            "code": self.code,
            "message": str(self),
            "task_id": self.task_id,
            "run_id": self.run_id,
            "context_row_id": self.context_row_id,
        }


class ConversationTaskStateRebuilder:
    """Rebuild a Task Transport snapshot from Task, Run, and context records only."""

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
            ConversationStateRebuildError: If records cross Task boundaries, reference an
                orphan Run/tool call, contain unsupported schema or metadata, or would produce
                an invalid snapshot.

        Side effects:
            None. This method does not access sessions, snapshot storage, checkpoints, tools,
            frontend runtime state, or the in-memory Transport registry.
        """

        run_records = list(runs)
        run_by_id: dict[int, ConversationRunRecord] = {}
        for run in run_records:
            if run.task_id != task.id:
                raise ConversationStateRebuildError(
                    "run_task_mismatch",
                    "run record belongs to another task",
                    task_id=task.id,
                    run_id=run.id,
                )
            if run.id in run_by_id:
                raise ConversationStateRebuildError(
                    "duplicate_run_id",
                    "run records contain a duplicate id",
                    task_id=task.id,
                    run_id=run.id,
                )
            run_by_id[run.id] = run

        rows = sorted(context_rows, key=lambda row: row.sequence)
        for row in rows:
            ConversationTaskStateRebuilder._validate_context_row(task.id, row, run_by_id)

        snapshot_runs: list[ConversationRunSnapshot] = []
        messages_by_run: dict[int, list[ConversationStateMessage]] = {}
        assistant_by_run: dict[int, ConversationStateMessage] = {}
        tool_parts: dict[str, tuple[int, dict[str, Any]]] = {}
        tool_rows: list[ConversationTaskContextRecord] = []
        settled_tool_call_ids: set[str] = set()

        for row in rows:
            message = row.message
            if isinstance(message, ToolMessage):
                tool_rows.append(row)
                continue
            if isinstance(message, SystemMessage):
                continue

            assert row.run_id is not None
            run_messages = messages_by_run.setdefault(row.run_id, [])
            if isinstance(message, HumanMessage):
                run_messages.append(
                    {
                        "id": str(row.id),
                        "role": "user",
                        "parts": [
                            {
                                "type": "text",
                                "text": content_to_text(message.content),
                                "status": "completed",
                            }
                        ],
                    }
                )
                continue

            if not isinstance(message, AIMessage):
                raise ConversationStateRebuildError(
                    "unsupported_context_message",
                    "context message type is unsupported",
                    task_id=task.id,
                    run_id=row.run_id,
                    context_row_id=row.id,
                )
            assistant = assistant_by_run.get(row.run_id)
            if assistant is None:
                assistant = cast(
                    ConversationStateMessage,
                    {"id": str(row.id), "role": "assistant", "parts": []},
                )
                assistant_by_run[row.run_id] = assistant
                run_messages.append(assistant)
            parts = assistant["parts"]
            for part in row.transport_metadata["parts"]:
                copied_part = cast(dict[str, Any], copy.deepcopy(dict(part)))
                if copied_part.get("type") == "tool-call":
                    call_id = copied_part.get("toolCallId")
                    if not isinstance(call_id, str) or not call_id:
                        raise ConversationStateRebuildError(
                            "malformed_context_metadata",
                            "tool-call metadata has an empty id",
                            task_id=task.id,
                            run_id=row.run_id,
                            context_row_id=row.id,
                        )
                    if call_id in tool_parts:
                        raise ConversationStateRebuildError(
                            "duplicate_tool_call_id",
                            "context metadata contains a duplicate tool-call id",
                            task_id=task.id,
                            run_id=row.run_id,
                            context_row_id=row.id,
                        )
                    tool_parts[call_id] = (row.run_id, copied_part)
                parts.append(cast(ConversationStatePart, copied_part))

        for row in tool_rows:
            assert row.run_id is not None
            call_id = cast(ToolMessage, row.message).tool_call_id
            if not isinstance(call_id, str) or not call_id:
                raise ConversationStateRebuildError(
                    "orphan_tool_message",
                    "tool message has no tool-call id",
                    task_id=task.id,
                    run_id=row.run_id,
                    context_row_id=row.id,
                )
            if call_id not in tool_parts:
                raise ConversationStateRebuildError(
                    "orphan_tool_message",
                    "tool message has no matching AI tool-call part",
                    task_id=task.id,
                    run_id=row.run_id,
                    context_row_id=row.id,
                )
            owner_run_id, part = tool_parts[call_id]
            if owner_run_id != row.run_id:
                raise ConversationStateRebuildError(
                    "tool_run_mismatch",
                    "tool message and AI tool-call belong to different runs",
                    task_id=task.id,
                    run_id=row.run_id,
                    context_row_id=row.id,
                )
            if call_id in settled_tool_call_ids:
                raise ConversationStateRebuildError(
                    "duplicate_tool_call_id",
                    "multiple tool messages reference one tool-call id",
                    task_id=task.id,
                    run_id=row.run_id,
                    context_row_id=row.id,
                )
            result = row.transport_metadata.get("tool_result")
            if not isinstance(result, dict):
                raise ConversationStateRebuildError(
                    "malformed_tool_result",
                    "tool message is missing a structured tool result",
                    task_id=task.id,
                    run_id=row.run_id,
                    context_row_id=row.id,
                )
            settled_tool_call_ids.add(call_id)
            ConversationTaskStateRebuilder._apply_tool_result(part, result)

        for call_id, (owner_run_id, part) in tool_parts.items():
            if call_id in settled_tool_call_ids:
                continue
            run_status = run_by_id[owner_run_id].status
            if run_status not in _TERMINAL_RUN_STATUSES:
                continue
            if run_status == "cancelled":
                part["status"] = "cancelled"
                part["error"] = "已取消"
                part["isError"] = False
            else:
                part["status"] = "failed"
                part["error"] = "执行异常"
                part["isError"] = True
            part.pop("display_data", None)
            part.pop("errorCode", None)

        for run in sorted(run_records, key=lambda item: (item.created_at, item.id)):
            snapshot_runs.append(
                {
                    "runId": run.id,
                    "status": run.status,
                    "endReason": run.end_reason,
                    "messages": messages_by_run.get(run.id, []),
                    "usage": copy.deepcopy(run.usage),
                }
            )

        used = task.context_usage_used
        total = task.context_window_total
        ratio = None if used is None or total is None or total == 0 else used / total
        state: ConversationStateSnapshot = {
            "runs": snapshot_runs,
            "current_run_id": task.current_run_id,
            "approvals": {},
            "context_usage_ratio": ratio,
            "context_usage_used": used,
            "context_window_total": total,
            "error": None,
        }
        try:
            validate_snapshot(state)
        except (TypeError, ValueError, KeyError) as exc:
            raise ConversationStateRebuildError(
                "invalid_snapshot",
                "rebuilt conversation state failed snapshot validation",
                task_id=task.id,
            ) from exc
        return state

    @staticmethod
    def _validate_context_row(
        task_id: int,
        row: ConversationTaskContextRecord,
        run_by_id: dict[int, ConversationRunRecord],
    ) -> None:
        """Validate row ownership, schema, message type, and persisted metadata."""

        if row.task_id != task_id:
            raise ConversationStateRebuildError(
                "context_task_mismatch",
                "context row belongs to another task",
                task_id=task_id,
                run_id=row.run_id,
                context_row_id=row.id,
            )
        if row.id is None:
            raise ConversationStateRebuildError(
                "context_row_missing_id",
                "persisted context row has no id",
                task_id=task_id,
                run_id=row.run_id,
            )
        if row.message_schema_version != _SUPPORTED_MESSAGE_SCHEMA_VERSION:
            raise ConversationStateRebuildError(
                "unsupported_context_schema",
                "context message schema version is unsupported",
                task_id=task_id,
                run_id=row.run_id,
                context_row_id=row.id,
            )
        try:
            metadata = validate_transport_metadata(row.transport_metadata)
        except (TypeError, ValueError, KeyError) as exc:
            raise ConversationStateRebuildError(
                "malformed_context_metadata",
                "context transport metadata is malformed",
                task_id=task_id,
                run_id=row.run_id,
                context_row_id=row.id,
            ) from exc
        if metadata["schema_version"] != _SUPPORTED_TRANSPORT_SCHEMA_VERSION:
            raise ConversationStateRebuildError(
                "unsupported_context_schema",
                "transport metadata schema version is unsupported",
                task_id=task_id,
                run_id=row.run_id,
                context_row_id=row.id,
            )
        if row.run_id is not None and row.run_id not in run_by_id:
            raise ConversationStateRebuildError(
                "orphan_context_run",
                "context row references an unknown run",
                task_id=task_id,
                run_id=row.run_id,
                context_row_id=row.id,
            )
        if not isinstance(row.message, HumanMessage | AIMessage | ToolMessage | SystemMessage):
            raise ConversationStateRebuildError(
                "unsupported_context_message",
                "context message type is unsupported",
                task_id=task_id,
                run_id=row.run_id,
                context_row_id=row.id,
            )
        if not isinstance(row.message, SystemMessage) and row.run_id is None:
            raise ConversationStateRebuildError(
                "context_message_without_run",
                "non-system context message has no run",
                task_id=task_id,
                context_row_id=row.id,
            )
        tool_result = metadata["tool_result"]
        message_name = type(row.message).__name__
        if isinstance(row.message, AIMessage) and tool_result is not None:
            raise ConversationStateRebuildError(
                "misplaced_transport_metadata",
                f"{message_name} context row cannot carry tool_result metadata",
                task_id=task_id,
                run_id=row.run_id,
                context_row_id=row.id,
            )
        if isinstance(row.message, ToolMessage) and (metadata["parts"] or tool_result is None):
            raise ConversationStateRebuildError(
                "misplaced_transport_metadata",
                f"{message_name} context row requires only tool_result metadata",
                task_id=task_id,
                run_id=row.run_id,
                context_row_id=row.id,
            )
        if isinstance(row.message, HumanMessage | SystemMessage) and (
            tool_result is not None or metadata["parts"]
        ):
            raise ConversationStateRebuildError(
                "misplaced_transport_metadata",
                f"{message_name} context row cannot carry tool UI metadata",
                task_id=task_id,
                run_id=row.run_id,
                context_row_id=row.id,
            )

    @staticmethod
    def _apply_tool_result(part: dict[str, Any], result: dict[str, Any]) -> None:
        """Project persisted tool result facts into safe Transport tool-call fields."""

        result_status = result["status"]
        part["status"] = {
            "success": "completed",
            "error": "failed",
            "cancelled": "cancelled",
        }[result_status]
        if result_status == "success":
            part["error"] = None
            part["isError"] = False
            part["display_data"] = copy.deepcopy(result["display_data"])
        elif result_status == "error":
            status_hint = normalize_status_hint(
                str(part["toolName"]), result["status_hint"]
            )
            part["error"] = status_hint
            part["isError"] = True
            part["display_data"] = {"status_hint": status_hint}
        else:
            part["error"] = "已取消"
            part["isError"] = False
            part["display_data"] = {"status_hint": "已取消"}
        if "errorCode" in result:
            part["errorCode"] = result["errorCode"]
        else:
            part.pop("errorCode", None)
__all__ = [
    "AgentContextLoader",
    "ConversationStateRebuildError",
    "ConversationTaskStateRebuilder",
    "load_agent_context",
]
