"""Pure reconstruction of Assistant Transport state from canonical conversation facts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import groupby
from operator import attrgetter
from typing import cast

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages.tool import ToolCall

from app.assistant_transport.event import build_user_input_parts
from app.assistant_transport.state.conversation_run_snapshot import ConversationRunSnapshot
from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_part import (
    ConversationStatePart,
    ConversationStateTextPart,
    ConversationStateToolCallPart,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
)
from app.config.configuration import get_agent_registry, get_tool_registry
from app.models.conversation_run_extra import ConversationRunExtra
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.delegation_record import DelegationRecord
from app.models.task_record import TaskRecord
from app.utils.message_content import content_to_text


class ConversationTaskStateRebuilder:
    """Rebuild a Task Transport snapshot from Task, Run, and context records only."""

    @staticmethod
    def build_pair_tool_part(
        rows: list[ConversationTaskContextRecord],
        delegations: Sequence[DelegationRecord] = (),
    ) -> dict[str, ConversationStateToolCallPart]:
        tool_parts: dict[str, ConversationStateToolCallPart] = {}
        for row in rows:
            message: BaseMessage = row.message
            if isinstance(message, AIMessage) and cast(AIMessage, message).tool_calls:
                ai_message = cast(AIMessage, message)
                calls: list[ToolCall] = ai_message.tool_calls
                for call in calls:
                    tool_part = ConversationStateToolCallPart(
                        type="tool-call",
                        toolCallId=call.get("id"),
                        toolName=call.get("name"),
                        args=call.get("args"),
                        presentation=ConversationTaskStateRebuilder.get_tool_display(
                            call.get("name")
                        ),
                        status="cancelled",
                    )
                    if call.get("name") == "delegate_task":
                        args = call.get("args") or {}
                        candidates = [
                            record
                            for record in delegations
                            if isinstance(record.child_task_id, int)
                            and record.child_task_id > 0
                            and record.child_agent_id == args.get("child_agent_id")
                            and record.prompt == args.get("prompt")
                        ]
                        if len(candidates) == 1:
                            record = candidates[0]
                            child_task_id = record.child_task_id
                            if child_task_id is not None and child_task_id > 0:
                                tool_part["child_task_id"] = child_task_id
                                child_run_id = record.child_run_id
                                if (
                                    isinstance(child_run_id, int)
                                    and not isinstance(child_run_id, bool)
                                    and child_run_id > 0
                                ):
                                    tool_part["child_run_id"] = child_run_id
                                tool_part["display_data"] = {
                                    "kind": "delegation-result",
                                    "title": args.get("title") or "子 Agent",
                                    "child_task_id": child_task_id,
                                }
                                if "child_run_id" in tool_part:
                                    tool_part["display_data"]["child_run_id"] = child_run_id
                                try:
                                    profile = get_agent_registry().resolve(record.child_agent_id)
                                except RuntimeError:
                                    profile = None
                                if profile is not None and profile.role.strip():
                                    tool_part["agent_role"] = profile.role
                                    display_data = tool_part.get("display_data")
                                    if display_data is not None:
                                        display_data["role"] = profile.role
                    tool_parts[call.get("id")] = tool_part
            if isinstance(message, ToolMessage):
                tool_message = cast(ToolMessage, message)
                tool_part: ConversationStateToolCallPart = tool_parts.get(tool_message.tool_call_id)
                if tool_part is None:
                    raise RuntimeError("未闭合tool")
                tool_part["status"] = row.transport_metadata.get("status")
                display_data = row.transport_metadata.get("display_data")
                tool_part["display_data"] = display_data
                if (
                    isinstance(display_data, dict)
                    and display_data.get("kind") == "delegation-result"
                ):
                    child_task_id = display_data.get("child_task_id")
                    if isinstance(child_task_id, int) and child_task_id > 0:
                        tool_part["child_task_id"] = child_task_id
                    child_run_id = display_data.get("child_run_id")
                    if (
                        isinstance(child_run_id, int)
                        and not isinstance(child_run_id, bool)
                        and child_run_id > 0
                    ):
                        tool_part["child_run_id"] = child_run_id
                    child_agent_id = display_data.get("child_agent_id")
                    if isinstance(child_agent_id, str) and child_agent_id:
                        try:
                            profile = get_agent_registry().resolve(child_agent_id)
                        except RuntimeError:
                            profile = None
                        if profile is not None and profile.role.strip():
                            tool_part["agent_role"] = profile.role
                            display_data = dict(display_data)
                            display_data["role"] = profile.role
                            tool_part["display_data"] = display_data
                if tool_part["status"] == "failed":
                    tool_part["isError"] = True
                    tool_part["error"] = row.transport_metadata.get("error")
                else:
                    tool_part["isError"] = False
                    tool_part["error"] = None

        return tool_parts

    @staticmethod
    def get_tool_display(tool_name: str) -> dict[str, object]:
        """Return the static display hints for ``tool_name`` as a plain dict.

        Parameters:
            tool_name: Registered tool name carried by the AI tool call.

        Returns:
            The ``ToolDisplayHints`` serialized to dict. Returns an **empty dict**
            (never ``None``) when ``tool_name`` is empty, the tool is not registered,
            or it declares no ``display``. This keeps the rebuilt snapshot part's
            ``presentation`` field a valid object so ``validate_snapshot`` accepts it,
            matching the streaming projection path which always emits ``{}`` for the
            same missing-display case.
        """
        if not tool_name:
            return {}
        tool_registry = get_tool_registry()
        tool_definition = tool_registry.get_tool_definition(tool_name)
        if not tool_definition or not tool_definition.display:
            return {}
        return tool_definition.display.to_dict()

    @staticmethod
    def build_user_message(run: ConversationRunRecord) -> ConversationStateMessage:
        """从 Run 输入事实构造可冷重建的 user 消息。

        正常执行会先写入 Human context；若进程恰好在该写入前崩溃，Run.extra 仍是已提交
        的普通附件事实，因此 snapshot 不能因为缺少 context 行而丢失用户消息或附件。
        """
        extra: ConversationRunExtra | None = getattr(run, "extra", None)
        text = extra.display_text if extra is not None else run.input_text
        user_parts: list[ConversationStatePart] = cast(
            list[ConversationStatePart],
            build_user_input_parts(
                text,
                getattr(run, "image_paths", None) or [],
                cast(
                    Sequence[Mapping[str, str]],
                    extra.attachments if extra is not None else [],
                ),
            ),
        )
        return ConversationStateMessage(
            id=f"user-{run.id}",
            role="user",
            parts=user_parts,
        )

    @staticmethod
    def rebuild(
        task: TaskRecord,
        runs: Sequence[ConversationRunRecord],
        context_rows: Sequence[ConversationTaskContextRecord],
        delegations: Sequence[DelegationRecord] = (),
    ) -> ConversationStateSnapshot:
        """Return a validated snapshot assembled from the three canonical record types.

        Parameters:
            task: Task-level current run and context usage facts.
            runs: All Run records belonging to ``task``.
            context_rows: Persisted LangChain messages and Transport metadata for the Task.

        Returns:
            A new ``ConversationStateSnapshot``. Run ordering is ``created_at`` then id;
            assistant rows for one Run are merged and all message ids come from context row ids.
            Streaming assistant drafts are retained in the assistant message; their text parts
            are ``running`` only when the owning Run is still active.

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

            tool_parts_dict = ConversationTaskStateRebuilder.build_pair_tool_part(
                rows,
                [record for record in delegations if record.parent_run_id == run.id],
            )

            snapshot_messages: list[ConversationStateMessage] = []
            assistant_message = ConversationStateMessage(
                id=f"assistant-{run.id}", role="assistant", parts=[]
            )
            has_user_message = False
            for row in rows:
                message = row.message
                if isinstance(message, HumanMessage):
                    has_user_message = True
                    snapshot_messages.append(ConversationTaskStateRebuilder.build_user_message(run))
                elif isinstance(message, AIMessage):
                    ai_message: AIMessage = cast(AIMessage, message)
                    text_status = (
                        "running"
                        if row.is_streaming and run.status in {"pending", "running"}
                        else "completed"
                    )
                    if ai_message.additional_kwargs.get("reasoning_content") is not None:
                        assistant_message["parts"].append(
                            ConversationStateTextPart(
                                type="reasoning",
                                text=content_to_text(
                                    ai_message.additional_kwargs["reasoning_content"]
                                ),
                                status=text_status,
                            )
                        )
                    if ai_message.content is not None:
                        assistant_message["parts"].append(
                            ConversationStateTextPart(
                                type="text",
                                text=content_to_text(ai_message.content),
                                status=text_status,
                            )
                        )
                    if ai_message.tool_calls is not None and len(ai_message.tool_calls) > 0:
                        calls: list[ToolCall] = ai_message.tool_calls
                        for call in calls:
                            assistant_message["parts"].append(tool_parts_dict[call.get("id")])
            run_extra: ConversationRunExtra | None = getattr(run, "extra", None)
            if not has_user_message and (
                getattr(run, "input_text", "").strip()
                or getattr(run, "image_paths", None)
                or (run_extra.attachments if run_extra is not None else [])
            ):
                snapshot_messages.append(ConversationTaskStateRebuilder.build_user_message(run))
            snapshot_messages.append(assistant_message)
            snapshot_runs.append(
                ConversationRunSnapshot(
                    runId=run.id,
                    status=run.status,
                    endReason=run.end_reason,
                    messages=snapshot_messages,
                    usage=run.usage,
                )
            )
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
            approvals={},
        )


__all__ = [
    "ConversationTaskStateRebuilder",
]
