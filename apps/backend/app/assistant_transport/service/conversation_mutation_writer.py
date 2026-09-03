"""Canonical conversation-fact mutations.

This module is the only service-level writer for the conversation projection.  It
does not publish request events and it does not know about Assistant UI types.
Every public mutation commits the fact in one SQLite write transaction.
"""

import copy
import json
from typing import cast

from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.orm import Session, aliased

from app.models.conversation_message_record import ConversationMessageRecord
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.models.conversation_tool_call_record import ConversationToolCallRecord
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.assistant_transport.service.conversation_snapshot_service import (
    ConversationStateMutation,
    ConversationTaskSnapshotService,
)
from app.storage.crud.conversation_message_crud import ConversationMessageCrud
from app.storage.crud.conversation_message_part_crud import ConversationMessagePartCrud
from app.storage.crud.conversation_tool_call_crud import ConversationToolCallCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.model.conversation_message_model import ConversationMessageModel
from app.storage.model.conversation_message_part_model import ConversationMessagePartModel
from app.storage.model.conversation_run_model import ConversationRunModel
from app.storage.model.conversation_tool_call_model import ConversationToolCallModel
from app.storage.model.human_approval_request_model import HumanApprovalRequestModel
from app.storage.model.task_model import TaskModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import to_text, utc_now


class ConversationMutationWriter:
    """Atomically write canonical conversation facts."""

    def __init__(self) -> None:
        """Bind the shared database session factory and fact CRUD objects."""

        self._session_factory = main_session_factory()
        self._task = TaskCrud()
        self._messages = ConversationMessageCrud()
        self._parts = ConversationMessagePartCrud()
        self._tool_calls = ConversationToolCallCrud()
        self._snapshots = ConversationTaskSnapshotService()

    def clear_run(self, run_id: int) -> None:
        """清理一次运行的可重建事实，并保留 user/assistant 基线消息。

        参数:
            run_id: 要重新执行的 Conversation Run 标识。

        返回:
            无。

        异常:
            KeyError: 运行不存在。
            sqlalchemy.exc.SQLAlchemyError: 清理或 canonical fact 写入失败。

            副作用:
            在 canonical writer 的单一事务中删除运行期消息、parts、tool calls，重置
            assistant 占位消息，并同步更新任务快照中的运行消息。
        """
        with self._write_session() as session:
            run = session.get(ConversationRunModel, run_id)
            if run is None:
                raise KeyError(run_id)
            messages = (
                session.execute(
                    select(ConversationMessageModel)
                    .where(ConversationMessageModel.run_id == run_id)
                    .order_by(ConversationMessageModel.sequence.asc())
                )
                .scalars()
                .all()
            )
            preserved_ids: set[int] = set()
            for role in ("user", "assistant"):
                role_messages = [message for message in messages if message.role == role]
                if role_messages:
                    preserved_ids.add(role_messages[0].id)
            discarded_ids = [message.id for message in messages if message.id not in preserved_ids]
            tool_call_filter = ConversationToolCallModel.run_id == run_id
            if discarded_ids:
                tool_call_filter = or_(
                    tool_call_filter,
                    ConversationToolCallModel.message_id.in_(discarded_ids),
                )
            session.execute(delete(ConversationToolCallModel).where(tool_call_filter))
            if discarded_ids:
                session.execute(
                    delete(ConversationMessagePartModel).where(
                        ConversationMessagePartModel.message_id.in_(discarded_ids)
                    )
                )
                session.execute(
                    delete(ConversationMessageModel).where(
                        ConversationMessageModel.id.in_(discarded_ids)
                    )
                )
            assistant = next(
                (
                    message
                    for message in messages
                    if message.id in preserved_ids and message.role == "assistant"
                ),
                None,
            )
            if assistant is not None:
                assistant.status = "running"
                assistant.end_reason = None
                parts = (
                    session.execute(
                        select(ConversationMessagePartModel)
                        .where(ConversationMessagePartModel.message_id == assistant.id)
                        .order_by(ConversationMessagePartModel.sequence.asc())
                    )
                    .scalars()
                    .all()
                )
                text_parts = [part for part in parts if part.part_type == "text"]
                if text_parts:
                    text_parts[0].text = ""
                    text_parts[0].status = "running"
                    for part in parts:
                        if part.id != text_parts[0].id:
                            session.delete(part)
                else:
                    session.add(
                        ConversationMessagePartModel(
                            message_id=assistant.id,
                            sequence=0,
                            part_type="text",
                            text="",
                            status="running",
                        )
                    )
            state = self._snapshot(run.task_id)
            preserved_message_ids = {f"message-{message_id}" for message_id in preserved_ids}
            snapshot_messages = []
            for message in state["messages"]:
                if message.get("runId") != run_id:
                    snapshot_messages.append(message)
                    continue
                if message["id"] not in preserved_message_ids:
                    continue
                if message["role"] != "assistant":
                    snapshot_messages.append(message)
                    continue
                reset_message = copy.deepcopy(message)
                reset_message["status"] = "running"
                reset_message["endReason"] = None
                text_part = next(
                    (part for part in reset_message["parts"] if part["type"] == "text"),
                    None,
                )
                if text_part is None:
                    reset_message["parts"] = [{"type": "text", "text": "", "status": "running"}]
                else:
                    reset_message["parts"] = [
                        {"type": "text", "text": "", "status": "running"}
                    ]
                snapshot_messages.append(reset_message)
            self._stage_snapshot(
                session,
                run.task_id,
                ConversationStateMutation("set", ("run", "runId"), run_id),
                ConversationStateMutation("set", ("run", "status"), "running"),
                ConversationStateMutation("set", ("messages",), snapshot_messages),
            )

    def record_approval_request(
        self, task_id: int, request_id: str, payload: object, run_id: int | None = None
    ) -> bool:
        """以 canonical fact 记录待人工审批请求。"""
        with self._write_session() as session:
            self._task.ensure_task(session, task_id)
            existing = session.execute(
                select(HumanApprovalRequestModel).where(
                    HumanApprovalRequestModel.task_id == task_id,
                    HumanApprovalRequestModel.request_id == request_id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return True
            request = HumanApprovalRequestModel(
                task_id=task_id,
                run_id=run_id,
                request_id=request_id,
                payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                status="pending",
            )
            session.add(request)
            session.flush()
            return True

    def resolve_approval(
        self,
        task_id: int,
        request_id: str,
        decision: str,
        reason: str | None = None,
        run_id: int | None = None,
    ) -> bool:
        """在同一事务中持久化审批决策。"""
        if decision not in {"approved", "rejected", "cancelled"}:
            raise ValueError(f"unsupported approval decision: {decision}")
        with self._write_session() as session:
            self._task.ensure_task(session, task_id)
            request = session.execute(
                select(HumanApprovalRequestModel).where(
                    HumanApprovalRequestModel.task_id == task_id,
                    HumanApprovalRequestModel.request_id == request_id,
                )
            ).scalar_one()
            request.status = "resolved"
            request.decision = decision
            request.decision_reason = reason
            session.flush()
            return True

    def create_message(
        self,
        task_id: int,
        role: str,
        run_id: int | None = None,
        status: str = "complete",
        end_reason: str | None = None,
        text: str | None = None,
        part_type: str = "text",
    ) -> tuple[ConversationMessageRecord, bool]:
        """Create a canonical message and optionally its first part atomically.

        参数:
            task_id: 所属 task 标识。
            role: system/user/assistant/tool 消息角色。
            run_id: 可选的运行标识。
            status: 消息状态。
            end_reason: 可选终止原因。
            text: 可选首个 part 文本。
            part_type: 首个 part 类型。

        返回:
            新消息与提交确认标记。

        异常:
            KeyError: task 不存在。
            ValueError: 角色或 part 参数非法。
            sqlalchemy.exc.SQLAlchemyError: 写事务失败。

        副作用:
            在同一写事务中创建消息、首个 part（如提供）并同步更新任务快照。
        """

        with self._write_session() as session:
            self._task.ensure_task(session, task_id)
            sequence = self._next_message_sequence(session, task_id)
            message = self._messages.create_in_session(
                session, task_id, sequence, role, run_id, status, end_reason
            )
            if text is not None:
                self._parts.create_in_session(session, message.id, 0, part_type, text)
            state = self._snapshot(task_id)
            message_path = ("messages", len(state["messages"]))
            part: dict[str, object] = {
                "type": part_type,
                "text": text or "",
                "status": "running" if status == "running" else "complete",
            }
            self._stage_snapshot(
                session,
                task_id,
                ConversationStateMutation(
                    "set",
                    message_path,
                    {
                        "id": f"message-{message.id}",
                        "runId": run_id,
                        "role": role,
                        "status": status,
                        "endReason": end_reason,
                        "createdAt": to_text(message.created_at),
                        "parts": [part],
                    },
                ),
                ConversationStateMutation("set", ("run", "runId"), run_id),
                ConversationStateMutation(
                    "set",
                    ("run", "status"),
                    "pending" if status == "running" else status,
                ),
            )
            return message, True

    def create_tool_message(
        self,
        task_id: int,
        run_id: int,
        tool_call_id: str,
        text: str,
    ) -> tuple[ConversationMessageRecord, bool]:
        """Create a model-facing tool message with an explicit call-id relation."""

        with self._write_session() as session:
            self._task.ensure_task(session, task_id)
            sequence = self._next_message_sequence(session, task_id)
            message = self._messages.create_in_session(
                session, task_id, sequence, "tool", run_id, "complete"
            )
            self._parts.create_in_session(
                session,
                message.id,
                0,
                "text",
                text,
                data_json=json.dumps(
                    {"toolCallId": tool_call_id}, ensure_ascii=False, sort_keys=True
                ),
            )
            return message, True

    def create_run_messages_in_session(
        self, session: Session, task_id: int, run_id: int, input_text: str
    ) -> tuple[ConversationMessageRecord, ConversationMessageRecord]:
        """Create the user message and assistant placeholder in an existing run transaction.

        参数:
            session: 调用方已经开启的写事务。
            task_id: 所属 task 标识。
            run_id: 运行标识。
            input_text: 用户消息文本。

        返回:
            ``(user_message, assistant_message)``；两条消息属于同一次原子事实提交。

        异常:
            KeyError: task 不存在。
            ValueError: 输入文本为空。
            sqlalchemy.exc.SQLAlchemyError: 写入失败。

        副作用:
            在调用方事务中创建用户消息、助手占位消息及其 text parts。
        """

        if not input_text.strip():
            raise ValueError("input_text must not be blank")
        self._task.ensure_task(session, task_id)
        user_sequence = self._next_message_sequence(session, task_id)
        user = self._messages.create_in_session(
            session, task_id, user_sequence, "user", run_id, "complete"
        )
        self._parts.create_in_session(session, user.id, 0, "text", input_text)
        assistant_sequence = self._next_message_sequence(session, task_id)
        assistant = self._messages.create_in_session(
            session, task_id, assistant_sequence, "assistant", run_id, "running"
        )
        self._parts.create_in_session(session, assistant.id, 0, "text", "", status="running")
        return user, assistant

    def append_text(
        self,
        task_id: int,
        part_id: int,
        text: str,
        run_id: int | None = None,
    ) -> bool:
        """Append model output text and commit one canonical mutation.

        参数:
            task_id: 所属 task 标识。
            part_id: 目标 text part 标识。
            text: 要追加的非空文本。
            run_id: 可选运行标识。

        返回:
            提交确认标记。

        异常:
            ValueError: 文本为空或目标不是 text part。
            KeyError: part 不存在。
            sqlalchemy.exc.SQLAlchemyError: 写事务失败。

        副作用:
            更新 part 文本，并向任务快照追加同样的文本。
        """

        with self._write_session() as session:
            self._task.ensure_task(session, task_id)
            self._ensure_active_run(session, run_id)
            self._parts.append_text_in_session(session, part_id, text)
            path = self._latest_text_path(task_id)
            if path is not None:
                self._stage_snapshot(
                    session,
                    task_id,
                    ConversationStateMutation("append-text", path, text),
                )
            return True

    def append_assistant_text_for_run(
        self,
        task_id: int,
        run_id: int,
        text: str,
    ) -> bool:
        """Append text to the canonical assistant part belonging to a run."""

        with self._write_session() as session:
            self._task.ensure_task(session, task_id)
            self._ensure_active_run(session, run_id)
            part = (
                session.execute(
                    select(ConversationMessagePartModel)
                    .join(
                        ConversationMessageModel,
                        ConversationMessageModel.id == ConversationMessagePartModel.message_id,
                    )
                    .where(
                        ConversationMessageModel.task_id == task_id,
                        ConversationMessageModel.run_id == run_id,
                        ConversationMessageModel.role == "assistant",
                        ConversationMessagePartModel.part_type == "text",
                    )
                    .order_by(ConversationMessagePartModel.id.desc())
                )
                .scalars()
                .first()
            )
            if part is None:
                raise KeyError(run_id)
            self._parts.append_text_in_session(session, part.id, text)
            path = self._assistant_part_path(task_id, run_id, "text")
            self._stage_snapshot(
                session,
                task_id,
                ConversationStateMutation("append-text", (*path, "text"), text),
            )
            return True

    def append_assistant_part_for_run(
        self,
        task_id: int,
        run_id: int,
        part_type: str,
        text: str,
    ) -> bool:
        """追加指定类型的助手 part（文本或推理）。"""
        if not text:
            raise ValueError("part text must not be empty")
        with self._write_session() as session:
            self._task.ensure_task(session, task_id)
            self._ensure_active_run(session, run_id)
            message = (
                session.execute(
                    select(ConversationMessageModel)
                    .where(
                        ConversationMessageModel.task_id == task_id,
                        ConversationMessageModel.run_id == run_id,
                        ConversationMessageModel.role == "assistant",
                    )
                    .order_by(ConversationMessageModel.id.desc())
                )
                .scalars()
                .first()
            )
            if message is None:
                raise KeyError(run_id)
            part = (
                session.execute(
                    select(ConversationMessagePartModel)
                    .where(
                        ConversationMessagePartModel.message_id == message.id,
                        ConversationMessagePartModel.part_type == part_type,
                    )
                    .order_by(ConversationMessagePartModel.sequence.desc())
                )
                .scalars()
                .first()
            )
            is_new_part = part is None
            if is_new_part:
                next_run_sequence = (
                    session.execute(
                        select(ConversationMessagePartModel.sequence)
                        .where(ConversationMessagePartModel.message_id == message.id)
                        .order_by(ConversationMessagePartModel.sequence.desc())
                    )
                    .scalars()
                    .first()
                )
                part = ConversationMessagePartModel(
                    message_id=message.id,
                    sequence=0 if next_run_sequence is None else next_run_sequence + 1,
                    part_type=part_type,
                    text=text,
                    status="running",
                )
                session.add(part)
            else:
                if part is None:
                    raise RuntimeError("existing conversation part disappeared")
                part.text = (part.text or "") + text
            session.flush()
            if is_new_part:
                message_path = self._assistant_message_path(task_id, run_id)
                state = self._snapshot(task_id)
                path = (
                    *message_path,
                    "parts",
                    len(state["messages"][cast(int, message_path[1])]["parts"]),
                )
                self._stage_snapshot(
                    session,
                    task_id,
                    ConversationStateMutation(
                        "set", path, {"type": part_type, "text": text, "status": "running"}
                    ),
                )
            else:
                path = self._assistant_part_path(task_id, run_id, part_type)
                self._stage_snapshot(
                    session,
                    task_id,
                    ConversationStateMutation("append-text", (*path, "text"), text),
                )
            return True

    def append_text_for_run(
        self,
        task_id: int,
        run_id: int,
        text: str,
    ) -> bool:
        """向指定运行的 assistant 文本 part 追加内容。

        运行启动时会先创建 assistant 占位消息；模型节点后续不能再次创建一条
        assistant 消息，而应把输出追加到该占位消息。本方法收口这一更新路径。
        """

        with self._write_session() as session:
            self._task.ensure_task(session, task_id)
            self._ensure_active_run(session, run_id)
            message = (
                session.execute(
                    select(ConversationMessageModel)
                    .where(
                        ConversationMessageModel.task_id == task_id,
                        ConversationMessageModel.run_id == run_id,
                        ConversationMessageModel.role == "assistant",
                    )
                    .order_by(ConversationMessageModel.id.desc())
                )
                .scalars()
                .first()
            )
            if message is None:
                raise KeyError(run_id)
            part = (
                session.execute(
                    select(ConversationMessagePartModel)
                    .where(
                        ConversationMessagePartModel.message_id == message.id,
                        ConversationMessagePartModel.part_type == "text",
                    )
                    .order_by(ConversationMessagePartModel.sequence.desc())
                )
                .scalars()
                .first()
            )
            if part is None:
                raise KeyError(message.id)
            part.text = (part.text or "") + text
            session.flush()
            path = self._assistant_part_path(task_id, run_id, "text")
            self._stage_snapshot(
                session,
                task_id,
                ConversationStateMutation("append-text", (*path, "text"), text),
            )
            return True

    def finish_assistant_for_run(
        self,
        task_id: int,
        run_id: int,
        status: str,
        end_reason: str | None = None,
    ) -> bool:
        """Set the canonical assistant message status for a run."""

        with self._write_session() as session:
            self._task.ensure_task(session, task_id)
            self._ensure_active_run(session, run_id)
            message = (
                session.execute(
                    select(ConversationMessageModel)
                    .where(
                        ConversationMessageModel.task_id == task_id,
                        ConversationMessageModel.run_id == run_id,
                        ConversationMessageModel.role == "assistant",
                    )
                    .order_by(ConversationMessageModel.id.desc())
                )
                .scalars()
                .first()
            )
            if message is None:
                raise KeyError(run_id)
            message.status = status
            message.end_reason = end_reason
            parts = (
                session.execute(
                    select(ConversationMessagePartModel)
                    .where(ConversationMessagePartModel.message_id == message.id)
                    .order_by(ConversationMessagePartModel.sequence.asc())
                )
                .scalars()
                .all()
            )
            for part in parts:
                if part.part_type in {"text", "reasoning"}:
                    part.status = "complete"
            session.flush()
            assistant_path = self._assistant_message_path(task_id, run_id)
            mutations = [
                ConversationStateMutation("set", (*assistant_path, "status"), status),
                ConversationStateMutation(
                    "set",
                    (*assistant_path, "endReason"),
                    end_reason,
                ),
            ]
            state = self._snapshot(task_id)
            message_state = state["messages"][cast(int, assistant_path[1])]
            mutations.extend(
                ConversationStateMutation(
                    "set",
                    (*assistant_path, "parts", index, "status"),
                    "complete",
                )
                for index, part in enumerate(message_state["parts"])
                if part["type"] in {"text", "reasoning"}
            )
            self._stage_snapshot(session, task_id, *mutations)
            return True

    def finish_message(
        self,
        task_id: int,
        message_id: int,
        status: str,
        end_reason: str | None = None,
    ) -> bool:
        """Set a message terminal status and publish its fact.

        参数:
            task_id: 所属 task 标识。
            message_id: 消息主键。
            status: complete/failed/cancelled 之一。
            end_reason: 可选终止原因。
            run_id: 可选运行标识。

        返回:
            提交确认标记。

        异常:
            ValueError: status 不是支持的终态。
            KeyError: task 或 message 不存在。
            sqlalchemy.exc.SQLAlchemyError: 写事务失败。

        副作用:
            更新消息状态、终止原因及任务快照中的对应状态。
        """

        if status not in {"complete", "failed", "cancelled"}:
            raise ValueError(f"unsupported message terminal status: {status}")
        with self._write_session() as session:
            self._task.ensure_task(session, task_id)
            message = session.get(ConversationMessageModel, message_id)
            if message is None or message.task_id != task_id:
                raise KeyError(message_id)
            message.status = status
            message.end_reason = end_reason
            session.flush()
            path = self._message_path(task_id, message_id)
            mutations = [
                ConversationStateMutation("set", (*path, "status"), status),
                ConversationStateMutation("set", (*path, "endReason"), end_reason),
            ]
            state = self._snapshot(task_id)
            mutations.extend(
                ConversationStateMutation("set", (*path, "parts", index, "status"), "complete")
                for index, part in enumerate(state["messages"][cast(int, path[1])]["parts"])
                if part["type"] in {"text", "reasoning"}
            )
            self._stage_snapshot(session, task_id, *mutations)
            return True

    def cancel_run(self, run_id: int, end_reason: str = "user_cancelled") -> bool | None:
        """以单一条件事务取消运行并落定其助手消息。

        参数:
            run_id: 当前与 ``conversation_commands.id`` 对应的 Conversation Run 标识。
            end_reason: 持久化的稳定取消原因。

        返回:
            成功完成条件转移时返回确认标记；运行不存在抛出 ``KeyError``，
            已处于非活动状态时返回 ``None``。

        异常:
            KeyError: 运行不存在。
            sqlalchemy.exc.SQLAlchemyError: 事务写入失败。

        副作用:
            在一个 SQLite 写事务内把 pending/running 转为 cancelled，更新对应
            assistant message。
        """
        with self._write_session() as session:
            run = session.get(ConversationRunModel, run_id)
            if run is None:
                raise KeyError(run_id)
            result = session.execute(
                update(ConversationRunModel)
                .where(
                    ConversationRunModel.id == run_id,
                    ConversationRunModel.status.in_(
                        (ConversationRunStatus.PENDING.value, ConversationRunStatus.RUNNING.value)
                    ),
                )
                .values(status="cancelled", end_reason=end_reason)
            )
            if not result.rowcount:
                return None
            assistant = (
                session.execute(
                    select(ConversationMessageModel)
                    .where(
                        ConversationMessageModel.run_id == run_id,
                        ConversationMessageModel.role == "assistant",
                    )
                    .order_by(ConversationMessageModel.id.desc())
                )
                .scalars()
                .first()
            )
            if assistant is not None:
                assistant.status = "cancelled"
                assistant.end_reason = end_reason
                assistant_parts = (
                    session.execute(
                        select(ConversationMessagePartModel).where(
                            ConversationMessagePartModel.message_id == assistant.id
                        )
                    )
                    .scalars()
                    .all()
                )
                for part in assistant_parts:
                    if part.part_type in {"text", "reasoning"}:
                        part.status = "complete"
                session.flush()
            session.execute(
                update(ConversationToolCallModel)
                .where(
                    ConversationToolCallModel.run_id == run_id,
                    ConversationToolCallModel.status.in_(("pending", "requires-action", "running")),
                )
                .values(status="cancelled", error_text=end_reason, updated_at=to_text(utc_now()))
            )
            mutations = [ConversationStateMutation("set", ("run", "status"), "cancelled")]
            if assistant is not None:
                assistant_path = self._assistant_message_path(run.task_id, run_id)
                mutations.extend(
                    [
                        ConversationStateMutation("set", (*assistant_path, "status"), "cancelled"),
                        ConversationStateMutation(
                            "set", (*assistant_path, "endReason"), end_reason
                        ),
                    ]
                )
                state = self._snapshot(run.task_id)
                mutations.extend(
                    ConversationStateMutation(
                        "set",
                        (*assistant_path, "parts", index, "status"),
                        "complete",
                    )
                    for index, part in enumerate(
                        state["messages"][cast(int, assistant_path[1])]["parts"]
                    )
                    if part["type"] in {"text", "reasoning"}
                )
            for tool_path in self._active_tool_paths(run.task_id, run_id):
                mutations.extend(
                    [
                        ConversationStateMutation("set", (*tool_path, "status"), "cancelled"),
                        ConversationStateMutation("set", (*tool_path, "error"), end_reason),
                        ConversationStateMutation("set", (*tool_path, "isError"), False),
                    ]
                )
            self._stage_snapshot(session, run.task_id, *mutations)
            return True

    def claim_pending_run(self, run_id: int) -> bool:
        """原子启动 pending run，并同步更新 Task snapshot 的运行状态。"""

        running_run = aliased(ConversationRunModel)
        with self._write_session() as session:
            run = session.get(ConversationRunModel, run_id)
            if run is None:
                raise KeyError(run_id)
            result = session.execute(
                update(ConversationRunModel)
                .where(
                    ConversationRunModel.id == run_id,
                    ConversationRunModel.status == ConversationRunStatus.PENDING.value,
                    ~exists(
                        select(1).where(
                            running_run.task_id == ConversationRunModel.task_id,
                            running_run.status == ConversationRunStatus.RUNNING.value,
                        )
                    ),
                )
                .values(
                    status=ConversationRunStatus.RUNNING.value,
                    updated_at=to_text(utc_now()),
                )
            )
            if not result.rowcount:
                return False
            self._stage_snapshot(
                session,
                run.task_id,
                ConversationStateMutation("set", ("run", "status"), "running"),
            )
            return True

    def settle_open_tool_calls(
        self,
        run_id: int,
        status: str,
        error_text: str,
    ) -> None:
        """Close every non-terminal tool call left by a run-level failure or cancellation."""

        if status not in {"failed", "cancelled"}:
            raise ValueError(f"unsupported open tool call terminal status: {status}")
        with self._write_session() as session:
            run = session.get(ConversationRunModel, run_id)
            if run is None:
                raise KeyError(run_id)
            self._ensure_active_run(session, run_id)
            session.execute(
                update(ConversationToolCallModel)
                .where(
                    ConversationToolCallModel.run_id == run_id,
                    ConversationToolCallModel.status.in_(("pending", "requires-action", "running")),
                )
                .values(status=status, error_text=error_text, updated_at=to_text(utc_now()))
            )
            mutations: list[ConversationStateMutation] = []
            for tool_path in self._active_tool_paths(run.task_id, run_id):
                mutations.extend(
                    [
                        ConversationStateMutation("set", (*tool_path, "status"), status),
                        ConversationStateMutation("set", (*tool_path, "error"), error_text),
                        ConversationStateMutation(
                            "set", (*tool_path, "isError"), status == "failed"
                        ),
                    ]
                )
            self._stage_snapshot(session, run.task_id, *mutations)

    def settle_run(
        self,
        run_id: int,
        status: str,
        *,
        end_reason: str | None = None,
    ) -> bool | None:
        """一次性落定运行与助手消息终态。

        参数:
            run_id: Conversation Run 标识；当前物理实现对应 ``conversation_commands.id``。
            status: ``completed``、``failed`` 或 ``cancelled``。
            end_reason: 可选的稳定终止原因。
        返回:
            成功转移时返回确认标记；运行已被其它执行者落定时返回 None。

        异常:
            ValueError: ``status`` 不是支持的终态。
            KeyError: 运行不存在。
            sqlalchemy.exc.SQLAlchemyError: 数据库事务失败。

            副作用:
            在同一 SQLite 写事务中条件更新 run、助手消息和任务快照，
            防止重复收尾或覆盖已经提交的终态。
        """
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError(f"unsupported run terminal status: {status}")
        with self._write_session() as session:
            run = session.get(ConversationRunModel, run_id)
            if run is None:
                raise KeyError(run_id)
            conditions = [
                ConversationRunModel.id == run_id,
                ConversationRunModel.status.in_(
                    (ConversationRunStatus.PENDING.value, ConversationRunStatus.RUNNING.value)
                ),
            ]
            result = session.execute(
                update(ConversationRunModel)
                .where(*conditions)
                .values(status=status, end_reason=end_reason)
            )
            if not result.rowcount:
                return None
            assistant = (
                session.execute(
                    select(ConversationMessageModel)
                    .where(
                        ConversationMessageModel.run_id == run_id,
                        ConversationMessageModel.role == "assistant",
                    )
                    .order_by(ConversationMessageModel.id.desc())
                )
                .scalars()
                .first()
            )
            if assistant is not None:
                assistant.status = status
                assistant.end_reason = end_reason
                assistant_parts = (
                    session.execute(
                        select(ConversationMessagePartModel).where(
                            ConversationMessagePartModel.message_id == assistant.id
                        )
                    )
                    .scalars()
                    .all()
                )
                for part in assistant_parts:
                    if part.part_type in {"text", "reasoning"}:
                        part.status = "complete"
                session.flush()
            mutations = [ConversationStateMutation("set", ("run", "status"), status)]
            if assistant is not None:
                assistant_path = self._assistant_message_path(run.task_id, run_id)
                mutations.extend(
                    [
                        ConversationStateMutation("set", (*assistant_path, "status"), status),
                        ConversationStateMutation(
                            "set", (*assistant_path, "endReason"), end_reason
                        ),
                    ]
                )
                state = self._snapshot(run.task_id)
                mutations.extend(
                    ConversationStateMutation(
                        "set",
                        (*assistant_path, "parts", index, "status"),
                        "complete",
                    )
                    for index, part in enumerate(
                        state["messages"][cast(int, assistant_path[1])]["parts"]
                    )
                    if part["type"] in {"text", "reasoning"}
                )
            self._stage_snapshot(session, run.task_id, *mutations)
            return True

    def create_tool_call(
        self,
        task_id: int,
        tool_call_id: str,
        tool_name: str,
        arguments: object,
        run_id: int | None = None,
        message_id: int | None = None,
        part_id: int | None = None,
    ) -> tuple[ConversationToolCallRecord, bool]:
        """Persist a structured tool-call fact and its snapshot mutation atomically."""

        with self._write_session() as session:
            self._task.ensure_task(session, task_id)
            self._ensure_active_run(session, run_id)
            existing_row = (
                session.execute(
                    select(ConversationToolCallModel).where(
                        ConversationToolCallModel.task_id == task_id,
                        ConversationToolCallModel.tool_call_id == tool_call_id,
                    )
                )
                .scalars()
                .first()
            )
            if existing_row is not None:
                existing = ConversationToolCallRecord.from_model(existing_row)
                normalized_args = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
                if existing.tool_name != tool_name or existing.args_json != normalized_args:
                    raise ValueError(
                        f"tool call id already exists with different payload: {tool_call_id}"
                    )
                return existing, True
            if message_id is None and run_id is not None:
                assistant = (
                    session.execute(
                        select(ConversationMessageModel)
                        .where(
                            ConversationMessageModel.task_id == task_id,
                            ConversationMessageModel.run_id == run_id,
                            ConversationMessageModel.role == "assistant",
                        )
                        .order_by(ConversationMessageModel.id.desc())
                    )
                    .scalars()
                    .first()
                )
                if assistant is not None:
                    message_id = assistant.id
            if message_id is not None and part_id is None:
                next_part_sequence = (
                    session.execute(
                        select(ConversationMessagePartModel.sequence)
                        .where(ConversationMessagePartModel.message_id == message_id)
                        .order_by(ConversationMessagePartModel.sequence.desc())
                    )
                    .scalars()
                    .first()
                )
                tool_part = ConversationMessagePartModel(
                    message_id=message_id,
                    sequence=0 if next_part_sequence is None else next_part_sequence + 1,
                    part_type="tool-call",
                    text=None,
                    data_json=json.dumps(
                        {"toolCallId": tool_call_id, "toolName": tool_name},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    status="pending",
                )
                session.add(tool_part)
                session.flush()
                part_id = tool_part.id
            call = self._tool_calls.create_in_session(
                session,
                task_id,
                tool_call_id,
                tool_name,
                json.dumps(arguments, ensure_ascii=False, sort_keys=True),
                run_id,
                message_id,
                part_id,
            )
            if message_id is not None:
                message_path = (
                    self._assistant_message_path(task_id, run_id or -1)
                    if run_id is not None
                    else self._message_path(task_id, message_id)
                )
                state = self._snapshot(task_id)
                tool_path = (
                    *message_path,
                    "parts",
                    len(state["messages"][cast(int, message_path[1])]["parts"]),
                )
                self._stage_snapshot(
                    session,
                    task_id,
                    ConversationStateMutation(
                        "set",
                        tool_path,
                        {
                            "type": "tool-call",
                            "toolCallId": tool_call_id,
                            "toolName": tool_name,
                            "status": "pending",
                            "args": arguments if isinstance(arguments, dict) else {},
                        },
                    ),
                )
            return call, True

    def transition_tool_call(
        self,
        task_id: int,
        tool_call_id: str,
        status: str,
        *,
        run_id: int | None = None,
    ) -> bool:
        """Atomically transition a tool call and publish the corresponding fact."""

        allowed = {
            "pending": {"running", "requires-action", "cancelled", "failed"},
            "requires-action": {"requires-action", "pending", "running", "cancelled", "failed"},
            "running": {"completed", "failed", "cancelled"},
        }
        valid = set(allowed) | {"completed", "failed", "cancelled"}
        if status not in valid:
            raise ValueError(f"unsupported tool call status: {status}")
        with self._write_session() as session:
            self._task.ensure_task(session, task_id)
            self._ensure_active_run(session, run_id)
            call = self._tool_calls.get_by_external_id_model_in_session(
                session, task_id, tool_call_id
            )
            current = call.status
            if current in {"completed", "failed", "cancelled"}:
                if current != status:
                    raise ValueError(f"tool call is already terminal: {tool_call_id}")
            elif status not in allowed.get(current, set()):
                raise ValueError(f"invalid tool call transition: {current} -> {status}")
            call.status = status
            call.updated_at = to_text(utc_now())
            session.flush()
            mutations = [
                ConversationStateMutation(
                    "set", (*self._tool_path(task_id, tool_call_id), "status"), status
                )
            ]
            if run_id is not None and status in {"requires-action", "running"}:
                mutations.append(
                    ConversationStateMutation(
                        "set",
                        (*self._assistant_message_path(task_id, run_id), "status"),
                        status,
                    )
                )
            self._stage_snapshot(session, task_id, *mutations)
            return True

    def complete_tool_call(
        self,
        task_id: int,
        call_id: int,
        result: object | None,
        status: str = "completed",
        error_text: str | None = None,
        run_id: int | None = None,
    ) -> bool:
        """Commit a tool-call terminal result and its snapshot mutation atomically."""

        with self._write_session() as session:
            self._task.ensure_task(session, task_id)
            self._ensure_active_run(session, run_id)
            completed = self._tool_calls.complete_in_session(
                session,
                call_id,
                None if result is None else json.dumps(result, ensure_ascii=False, sort_keys=True),
                status,
                error_text,
            )
            tool_path = self._tool_path(task_id, completed.tool_call_id)
            self._stage_tool_result(session, task_id, tool_path, result, status, error_text)
            return True

    def complete_tool_call_by_external_id(
        self,
        task_id: int,
        tool_call_id: str,
        result: object | None,
        status: str = "completed",
        error_text: str | None = None,
        run_id: int | None = None,
    ) -> bool:
        """按模型调用 ID 原子收口工具调用事实。"""
        with self._write_session() as session:
            self._task.ensure_task(session, task_id)
            self._ensure_active_run(session, run_id)
            existing = self._tool_calls.get_by_external_id_in_session(
                session, task_id, tool_call_id
            )
            self._tool_calls.complete_in_session(
                session,
                existing.id,
                None if result is None else json.dumps(result, ensure_ascii=False, sort_keys=True),
                status,
                error_text,
            )
            self._stage_tool_result(
                session,
                task_id,
                self._tool_path(task_id, tool_call_id),
                result,
                status,
                error_text,
            )
            return True

    def _stage_snapshot(
        self,
        session: Session,
        task_id: int,
        *mutations: ConversationStateMutation,
    ) -> None:
        """把已明确的领域 mutation 交给快照 owner 在当前事务中暂存。"""

        if mutations:
            self._snapshots.stage(session, task_id, mutations)

    def _snapshot(self, task_id: int) -> ConversationStateSnapshot:
        """读取当前 Task 快照，缺失时报告未初始化状态。"""

        state = self._snapshots.load(task_id)
        if state is None:
            state = {
                "messages": [],
                "run": {"runId": None, "status": "idle"},
                "error": None,
            }
            self._snapshots.hydrate(task_id, state)
        return state

    def _assistant_message_path(self, task_id: int, run_id: int) -> tuple[str | int, ...]:
        """返回指定 run 的 assistant message 路径。"""

        state = self._snapshot(task_id)
        for index in range(len(state["messages"]) - 1, -1, -1):
            message = state["messages"][index]
            if message["role"] == "assistant" and (
                state["run"]["runId"] == run_id or index == len(state["messages"]) - 1
            ):
                return ("messages", index)
        raise KeyError(run_id)

    def _assistant_part_path(
        self, task_id: int, run_id: int, part_type: str
    ) -> tuple[str | int, ...]:
        """返回指定 run 的 assistant part 路径。"""

        message_path = self._assistant_message_path(task_id, run_id)
        state = self._snapshot(task_id)
        message = state["messages"][cast(int, message_path[1])]
        for index in range(len(message["parts"]) - 1, -1, -1):
            if message["parts"][index]["type"] == part_type:
                return (*message_path, "parts", index)
        raise KeyError(part_type)

    def _latest_text_path(self, task_id: int) -> tuple[str | int, ...] | None:
        """返回快照中最后一个可追加文本 part 的路径。"""

        state = self._snapshot(task_id)
        for message_index in range(len(state["messages"]) - 1, -1, -1):
            parts = state["messages"][message_index]["parts"]
            for part_index in range(len(parts) - 1, -1, -1):
                if parts[part_index]["type"] == "text":
                    return ("messages", message_index, "parts", part_index, "text")
        return None

    def _message_path(self, task_id: int, message_id: int) -> tuple[str | int, ...]:
        """按持久化 message id 返回快照消息路径。"""

        state = self._snapshot(task_id)
        wanted = f"message-{message_id}"
        for index, message in enumerate(state["messages"]):
            if message["id"] == wanted:
                return ("messages", index)
        raise KeyError(message_id)

    def _tool_path(self, task_id: int, tool_call_id: str) -> tuple[str | int, ...]:
        """按外部 tool call id 返回快照 part 路径。"""

        state = self._snapshot(task_id)
        for message_index, message in enumerate(state["messages"]):
            for part_index, part in enumerate(message["parts"]):
                if part.get("type") == "tool-call" and part.get("toolCallId") == tool_call_id:
                    return ("messages", message_index, "parts", part_index)
        raise KeyError(tool_call_id)

    def _active_tool_paths(
        self, task_id: int, run_id: int | None = None
    ) -> list[tuple[str | int, ...]]:
        """返回指定 run 尚未终态的 tool part 路径。"""

        state = self._snapshot(task_id)
        paths: list[tuple[str | int, ...]] = []
        for message_index, message in enumerate(state["messages"]):
            if run_id is not None and message.get("runId") != run_id:
                continue
            for part_index, part in enumerate(message["parts"]):
                if part.get("type") == "tool-call" and part.get("status") in {
                    "pending",
                    "requires-action",
                    "running",
                }:
                    paths.append(("messages", message_index, "parts", part_index))
        return paths

    def _stage_tool_result(
        self,
        session: Session,
        task_id: int,
        path: tuple[str | int, ...],
        result: object | None,
        status: str,
        error_text: str | None,
    ) -> None:
        """把 tool 结果、错误和终态以最小路径 mutation 写入快照。"""

        self._stage_snapshot(
            session,
            task_id,
            ConversationStateMutation("set", (*path, "result"), result),
            ConversationStateMutation("set", (*path, "status"), status),
            ConversationStateMutation("set", (*path, "error"), error_text),
            ConversationStateMutation("set", (*path, "isError"), status == "failed"),
        )

    def _write_session(self) -> "_SessionContext":
        """Open an immediate SQLite write transaction for an atomic mutation."""

        session = self._session_factory()
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        return _SessionContext(session, self._snapshots)

    @staticmethod
    def _ensure_active_run(session: Session, run_id: int | None) -> None:
        """校验指定 run 存在且仍处于可写的 active 状态。"""
        if run_id is None:
            return
        run = session.get(ConversationRunModel, run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status not in {"pending", "running"}:
            raise PermissionError(f"run is no longer active: {run_id}")

    def _next_message_sequence(self, session: Session, task_id: int) -> int:
        """Increment and return the task-local message sequence."""

        task = session.get(TaskModel, task_id)
        if task is None:
            raise KeyError(task_id)
        task.message_sequence += 1
        session.flush()
        return task.message_sequence


class _SessionContext:
    """Commit/rollback adapter for an already-started immediate session transaction."""

    def __init__(self, session: Session, snapshots: ConversationTaskSnapshotService) -> None:
        self._session = session
        self._snapshots = snapshots

    def __enter__(self) -> Session:
        return self._session

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        try:
            if exc_type is None:
                self._session.commit()
                self._snapshots.publish_transaction(self._session)
            else:
                self._session.rollback()
        finally:
            self._session.close()
