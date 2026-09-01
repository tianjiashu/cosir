"""Canonical conversation-fact mutations.

This module is the only service-level writer for the conversation projection.  It
does not publish transport events and it does not know about Assistant UI types.
Every public mutation commits the fact and its task revision in one SQLite write
transaction.
"""

import json
from dataclasses import dataclass

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from app.models.conversation_change_record import ConversationChangeRecord
from app.models.conversation_message_record import ConversationMessageRecord
from app.models.conversation_tool_call_record import ConversationToolCallRecord
from app.storage.crud.conversation_change_crud import ConversationChangeCrud
from app.storage.crud.conversation_head_crud import ConversationHeadCrud
from app.storage.crud.conversation_message_crud import ConversationMessageCrud
from app.storage.crud.conversation_message_part_crud import ConversationMessagePartCrud
from app.storage.crud.conversation_tool_call_crud import ConversationToolCallCrud
from app.storage.model.conversation_command_model import ConversationCommandModel
from app.storage.model.conversation_message_model import ConversationMessageModel
from app.storage.model.conversation_message_part_model import ConversationMessagePartModel
from app.storage.model.conversation_tool_call_model import ConversationToolCallModel
from app.storage.model.human_approval_request_model import HumanApprovalRequestModel
from app.storage.model.task_model import TaskModel
from app.storage.model.turn_model import TurnModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import to_text, utc_now


@dataclass(frozen=True)
class ConversationMutationResult:
    """Result of a committed canonical fact mutation."""

    revision: int
    change: ConversationChangeRecord


class ConversationMutationWriter:
    """Atomically write conversation facts and allocate task revisions."""

    def __init__(self) -> None:
        """Bind the shared database session factory and fact CRUD objects."""

        self._session_factory = main_session_factory()
        self._heads = ConversationHeadCrud()
        self._changes = ConversationChangeCrud()
        self._messages = ConversationMessageCrud()
        self._parts = ConversationMessagePartCrud()
        self._tool_calls = ConversationToolCallCrud()

    def clear_run(self, run_id: int) -> None:
        """清理一次运行的可重建事实，并保留 user/assistant 基线消息。

        参数:
            run_id: 要重新执行的 Conversation Run 标识。

        返回:
            无。

        异常:
            KeyError: 运行不存在。
            sqlalchemy.exc.SQLAlchemyError: 清理或 revision 写入失败。

        副作用:
            在 canonical writer 的单一事务中删除运行期消息、parts、tool calls，重置
            assistant 占位消息，并追加一条 ``messages_cleared`` change。
        """
        with self._write_session() as session:
            turn = session.get(TurnModel, run_id)
            if turn is None:
                raise KeyError(run_id)
            messages = (
                session.execute(
                    select(ConversationMessageModel)
                    .where(ConversationMessageModel.turn_id == run_id)
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
            session.execute(
                delete(ConversationToolCallModel).where(
                    or_(
                        ConversationToolCallModel.turn_id == run_id,
                        ConversationToolCallModel.message_id.in_(discarded_ids)
                        if discarded_ids
                        else False,
                    )
                )
            )
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
            self._record_change(session, turn.task_id, "messages_cleared", "turn", run_id, run_id)

    def record_approval_request(
        self, task_id: int, request_id: str, payload: object, turn_id: int | None = None
    ) -> ConversationMutationResult:
        """以 canonical fact 记录待人工审批请求。"""
        with self._write_session() as session:
            self._ensure_task(session, task_id)
            existing = session.execute(
                select(HumanApprovalRequestModel).where(
                    HumanApprovalRequestModel.task_id == task_id,
                    HumanApprovalRequestModel.request_id == request_id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return self._record_change(
                    session, task_id, "approval_request_seen", "approval", existing.id, turn_id
                )
            request = HumanApprovalRequestModel(
                task_id=task_id,
                turn_id=turn_id,
                request_id=request_id,
                payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                status="pending",
            )
            session.add(request)
            session.flush()
            return self._record_change(
                session, task_id, "approval_requested", "approval", request.id, turn_id
            )

    def resolve_approval(
        self,
        task_id: int,
        request_id: str,
        decision: str,
        reason: str | None = None,
        turn_id: int | None = None,
    ) -> ConversationMutationResult:
        """在同一事务中持久化审批决策并生成 revision。"""
        if decision not in {"approved", "rejected", "cancelled"}:
            raise ValueError(f"unsupported approval decision: {decision}")
        with self._write_session() as session:
            self._ensure_task(session, task_id)
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
            return self._record_change(
                session, task_id, "approval_resolved", "approval", request.id, turn_id
            )

    def create_message(
        self,
        task_id: int,
        role: str,
        turn_id: int | None = None,
        status: str = "complete",
        end_reason: str | None = None,
        text: str | None = None,
        part_type: str = "text",
    ) -> tuple[ConversationMessageRecord, ConversationMutationResult]:
        """Create a canonical message and optionally its first part atomically.

        参数:
            task_id: 所属 task 标识。
            role: system/user/assistant/tool 消息角色。
            turn_id: 可选的运行标识。
            status: 消息状态。
            end_reason: 可选终止原因。
            text: 可选首个 part 文本。
            part_type: 首个 part 类型。

        返回:
            新消息与已提交 revision/change。

        异常:
            KeyError: task 不存在。
            ValueError: 角色或 part 参数非法。
            sqlalchemy.exc.SQLAlchemyError: 写事务失败。

        副作用:
            在同一写事务中创建消息、首个 part（如提供）和 change 索引。
        """

        with self._write_session() as session:
            self._ensure_task(session, task_id)
            sequence = self._next_message_sequence(session, task_id)
            message = self._messages.create_in_session(
                session, task_id, sequence, role, turn_id, status, end_reason
            )
            if text is not None:
                self._parts.create_in_session(session, message.id, 0, part_type, text)
            result = self._record_change(
                session, task_id, "message_created", "message", message.id, turn_id
            )
            return message, result

    def create_tool_message(
        self,
        task_id: int,
        turn_id: int,
        tool_call_id: str,
        text: str,
    ) -> tuple[ConversationMessageRecord, ConversationMutationResult]:
        """Create a model-facing tool message with an explicit call-id relation."""

        with self._write_session() as session:
            self._ensure_task(session, task_id)
            sequence = self._next_message_sequence(session, task_id)
            message = self._messages.create_in_session(
                session, task_id, sequence, "tool", turn_id, "complete"
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
            result = self._record_change(
                session, task_id, "tool_message_created", "message", message.id, turn_id
            )
            return message, result

    def create_run_messages_in_session(
        self, session: Session, task_id: int, turn_id: int, input_text: str
    ) -> tuple[ConversationMessageRecord, ConversationMessageRecord, int]:
        """Create the user message and assistant placeholder in an existing run transaction.

        参数:
            session: 调用方已经开启的写事务。
            task_id: 所属 task 标识。
            turn_id: 运行标识。
            input_text: 用户消息文本。

        返回:
            ``(user_message, assistant_message, revision)``；两条消息和一个 revision
            属于同一次原子事实提交。

        异常:
            KeyError: task 不存在。
            ValueError: 输入文本为空。
            sqlalchemy.exc.SQLAlchemyError: 写入失败。

        副作用:
            在调用方事务中创建用户消息、助手占位消息及其 text parts，并写入一个 change。
        """

        if not input_text.strip():
            raise ValueError("input_text must not be blank")
        self._ensure_task(session, task_id)
        user_sequence = self._next_message_sequence(session, task_id)
        user = self._messages.create_in_session(
            session, task_id, user_sequence, "user", turn_id, "complete"
        )
        self._parts.create_in_session(session, user.id, 0, "text", input_text)
        assistant_sequence = self._next_message_sequence(session, task_id)
        assistant = self._messages.create_in_session(
            session, task_id, assistant_sequence, "assistant", turn_id, "running"
        )
        self._parts.create_in_session(session, assistant.id, 0, "text", "", status="running")
        mutation = self._record_change(
            session, task_id, "run_messages_created", "turn", turn_id, turn_id
        )
        return user, assistant, mutation.revision

    def append_text(
        self,
        task_id: int,
        part_id: int,
        text: str,
        turn_id: int | None = None,
        fencing_version: int | None = None,
    ) -> ConversationMutationResult:
        """Append model output text and commit one revision.

        参数:
            task_id: 所属 task 标识，用于隔离 revision。
            part_id: 目标 text part 标识。
            text: 要追加的非空文本。
            turn_id: 可选运行标识。

        返回:
            已提交 revision/change。

        异常:
            ValueError: 文本为空或目标不是 text part。
            KeyError: part 不存在。
            sqlalchemy.exc.SQLAlchemyError: 写事务失败。

        副作用:
            更新 part 文本并写入 change 索引。
        """

        with self._write_session() as session:
            self._ensure_task(session, task_id)
            self._ensure_fencing(session, turn_id, fencing_version)
            self._parts.append_text_in_session(session, part_id, text)
            return self._record_change(
                session, task_id, "message_text_appended", "message_part", part_id, turn_id
            )

    def append_assistant_text_for_turn(
        self,
        task_id: int,
        turn_id: int,
        text: str,
        fencing_version: int | None = None,
    ) -> ConversationMutationResult:
        """Append text to the canonical assistant part belonging to a run."""

        with self._write_session() as session:
            self._ensure_task(session, task_id)
            self._ensure_fencing(session, turn_id, fencing_version)
            part = (
                session.execute(
                    select(ConversationMessagePartModel)
                    .join(
                        ConversationMessageModel,
                        ConversationMessageModel.id == ConversationMessagePartModel.message_id,
                    )
                    .where(
                        ConversationMessageModel.task_id == task_id,
                        ConversationMessageModel.turn_id == turn_id,
                        ConversationMessageModel.role == "assistant",
                        ConversationMessagePartModel.part_type == "text",
                    )
                    .order_by(ConversationMessagePartModel.id.desc())
                )
                .scalars()
                .first()
            )
            if part is None:
                raise KeyError(turn_id)
            self._parts.append_text_in_session(session, part.id, text)
            return self._record_change(
                session, task_id, "message_text_appended", "message_part", part.id, turn_id
            )

    def append_assistant_part_for_turn(
        self,
        task_id: int,
        turn_id: int,
        part_type: str,
        text: str,
        fencing_version: int | None = None,
    ) -> ConversationMutationResult:
        """追加指定类型的助手 part（文本或推理）并提交一个 revision。"""
        if not text:
            raise ValueError("part text must not be empty")
        with self._write_session() as session:
            self._ensure_task(session, task_id)
            self._ensure_fencing(session, turn_id, fencing_version)
            message = (
                session.execute(
                    select(ConversationMessageModel)
                    .where(
                        ConversationMessageModel.task_id == task_id,
                        ConversationMessageModel.turn_id == turn_id,
                        ConversationMessageModel.role == "assistant",
                    )
                    .order_by(ConversationMessageModel.id.desc())
                )
                .scalars()
                .first()
            )
            if message is None:
                raise KeyError(turn_id)
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
            if part is None:
                next_sequence = (
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
                    sequence=0 if next_sequence is None else next_sequence + 1,
                    part_type=part_type,
                    text=text,
                    status="running",
                )
                session.add(part)
            else:
                part.text = (part.text or "") + text
            session.flush()
            return self._record_change(
                session, task_id, "message_part_appended", "message_part", part.id, turn_id
            )

    def append_text_for_turn(
        self,
        task_id: int,
        turn_id: int,
        text: str,
        fencing_version: int | None = None,
    ) -> ConversationMutationResult:
        """向指定运行的 assistant 文本 part 追加内容。

        运行启动时会先创建 assistant 占位消息；模型节点后续不能再次创建一条
        assistant 消息，而应把输出追加到该占位消息。本方法收口这一更新路径。
        """

        with self._write_session() as session:
            self._ensure_task(session, task_id)
            self._ensure_fencing(session, turn_id, fencing_version)
            message = (
                session.execute(
                    select(ConversationMessageModel)
                    .where(
                        ConversationMessageModel.task_id == task_id,
                        ConversationMessageModel.turn_id == turn_id,
                        ConversationMessageModel.role == "assistant",
                    )
                    .order_by(ConversationMessageModel.id.desc())
                )
                .scalars()
                .first()
            )
            if message is None:
                raise KeyError(turn_id)
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
            return self._record_change(
                session, task_id, "message_part_appended", "message_part", part.id, turn_id
            )

    def finish_assistant_for_turn(
        self,
        task_id: int,
        turn_id: int,
        status: str,
        end_reason: str | None = None,
        fencing_version: int | None = None,
    ) -> ConversationMutationResult:
        """Set the canonical assistant message status for a run."""

        with self._write_session() as session:
            self._ensure_task(session, task_id)
            self._ensure_fencing(session, turn_id, fencing_version)
            message = (
                session.execute(
                    select(ConversationMessageModel)
                    .where(
                        ConversationMessageModel.task_id == task_id,
                        ConversationMessageModel.turn_id == turn_id,
                        ConversationMessageModel.role == "assistant",
                    )
                    .order_by(ConversationMessageModel.id.desc())
                )
                .scalars()
                .first()
            )
            if message is None:
                raise KeyError(turn_id)
            message.status = status
            message.end_reason = end_reason
            session.flush()
            return self._record_change(
                session, task_id, "message_finished", "message", message.id, turn_id
            )

    def finish_message(
        self,
        task_id: int,
        message_id: int,
        status: str,
        end_reason: str | None = None,
        turn_id: int | None = None,
    ) -> ConversationMutationResult:
        """Set a message terminal status and publish its fact revision.

        参数:
            task_id: 所属 task 标识。
            message_id: 消息主键。
            status: complete/failed/cancelled 之一。
            end_reason: 可选终止原因。
            turn_id: 可选运行标识。

        返回:
            已提交 revision/change。

        异常:
            ValueError: status 不是支持的终态。
            KeyError: task 或 message 不存在。
            sqlalchemy.exc.SQLAlchemyError: 写事务失败。

        副作用:
            更新消息状态与终止原因并写入 change 索引。
        """

        if status not in {"complete", "failed", "cancelled"}:
            raise ValueError(f"unsupported message terminal status: {status}")
        with self._write_session() as session:
            self._ensure_task(session, task_id)
            message = session.get(self._messages_model, message_id)
            if message is None or message.task_id != task_id:
                raise KeyError(message_id)
            message.status = status
            message.end_reason = end_reason
            session.flush()
            return self._record_change(
                session, task_id, "message_finished", "message", message_id, turn_id
            )

    def cancel_run(
        self, run_id: int, end_reason: str = "user_cancelled"
    ) -> ConversationMutationResult | None:
        """以单一条件事务取消运行并落定其助手消息。

        参数:
            run_id: 当前与 ``turns.id`` 对应的 Conversation Run 标识。
            end_reason: 持久化的稳定取消原因。

        返回:
            成功完成条件转移时返回 revision/change；运行不存在抛出 ``KeyError``，
            已处于非活动状态时返回 ``None``。

        异常:
            KeyError: 运行不存在。
            sqlalchemy.exc.SQLAlchemyError: 事务写入失败。

        副作用:
            在一个 SQLite 写事务内把 pending/running 转为 cancelled，更新对应
            assistant message，并递增 conversation revision。
        """
        with self._write_session() as session:
            turn = session.get(TurnModel, run_id)
            if turn is None:
                raise KeyError(run_id)
            result = session.execute(
                update(TurnModel)
                .where(
                    TurnModel.id == run_id,
                    TurnModel.status.in_(("pending", "running")),
                )
                .values(status="cancelled", end_reason=end_reason)
            )
            if not result.rowcount:
                return None
            assistant = (
                session.execute(
                    select(ConversationMessageModel)
                    .where(
                        ConversationMessageModel.turn_id == run_id,
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
                session.flush()
            session.execute(
                update(ConversationToolCallModel)
                .where(
                    ConversationToolCallModel.turn_id == run_id,
                    ConversationToolCallModel.status.in_(
                        ("pending", "requires-action", "running")
                    ),
                )
                .values(status="cancelled", error_text=end_reason, updated_at=to_text(utc_now()))
            )
            session.execute(
                update(ConversationCommandModel)
                .where(
                    ConversationCommandModel.turn_id == run_id,
                    ConversationCommandModel.status == "processing",
                )
                .values(status="cancelled")
            )
            return self._record_change(
                session,
                turn.task_id,
                "run_cancelled",
                "turn",
                run_id,
                run_id,
            )

    def settle_open_tool_calls(
        self,
        run_id: int,
        status: str,
        error_text: str,
        fencing_version: int | None = None,
    ) -> None:
        """Close every non-terminal tool call left by a run-level failure or cancellation."""

        if status not in {"failed", "cancelled"}:
            raise ValueError(f"unsupported open tool call terminal status: {status}")
        with self._write_session() as session:
            turn = session.get(TurnModel, run_id)
            if turn is None:
                raise KeyError(run_id)
            self._ensure_fencing(session, run_id, fencing_version)
            session.execute(
                update(ConversationToolCallModel)
                .where(
                    ConversationToolCallModel.turn_id == run_id,
                    ConversationToolCallModel.status.in_(
                        ("pending", "requires-action", "running")
                    ),
                )
                .values(status=status, error_text=error_text, updated_at=to_text(utc_now()))
            )
            self._record_change(session, turn.task_id, "tool_calls_settled", "turn", run_id, run_id)

    def settle_run(
        self,
        run_id: int,
        status: str,
        *,
        end_reason: str | None = None,
        fencing_version: int | None = None,
    ) -> ConversationMutationResult | None:
        """以 fencing 条件一次性落定运行与助手消息终态。

        参数:
            run_id: Conversation Run 标识；当前物理实现对应 ``turns.id``。
            status: ``completed``、``failed`` 或 ``cancelled``。
            end_reason: 可选的稳定终止原因。
            fencing_version: 执行租约版本；提供时只允许当前 lease 提交。

        返回:
            成功转移时返回已提交 revision/change；运行已被其它执行者落定时返回 None。

        异常:
            ValueError: ``status`` 不是支持的终态。
            KeyError: 运行不存在。
            sqlalchemy.exc.SQLAlchemyError: 数据库事务失败。

        副作用:
            在同一 SQLite 写事务中条件更新 run、助手消息和 conversation change，
            防止过期 executor 覆盖新 executor 或用户取消已经提交的终态。
        """
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError(f"unsupported run terminal status: {status}")
        with self._write_session() as session:
            turn = session.get(TurnModel, run_id)
            if turn is None:
                raise KeyError(run_id)
            conditions = [
                TurnModel.id == run_id,
                TurnModel.status.in_(("pending", "running")),
            ]
            if fencing_version is not None:
                conditions.append(TurnModel.fencing_version == fencing_version)
            result = session.execute(
                update(TurnModel).where(*conditions).values(status=status, end_reason=end_reason)
            )
            if not result.rowcount:
                return None
            assistant = (
                session.execute(
                    select(ConversationMessageModel)
                    .where(
                        ConversationMessageModel.turn_id == run_id,
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
                session.flush()
            session.execute(
                update(ConversationCommandModel)
                .where(
                    ConversationCommandModel.turn_id == run_id,
                    ConversationCommandModel.status == "processing",
                )
                .values(status=status)
            )
            return self._record_change(session, turn.task_id, "run_settled", "turn", run_id, run_id)

    def create_tool_call(
        self,
        task_id: int,
        tool_call_id: str,
        tool_name: str,
        arguments: object,
        turn_id: int | None = None,
        message_id: int | None = None,
        part_id: int | None = None,
        fencing_version: int | None = None,
    ) -> tuple[ConversationToolCallRecord, ConversationMutationResult]:
        """Persist a structured tool-call fact and its change index atomically."""

        with self._write_session() as session:
            self._ensure_task(session, task_id)
            self._ensure_fencing(session, turn_id, fencing_version)
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
                return existing, self._record_change(
                    session, task_id, "tool_call_seen", "tool_call", existing.id, turn_id
                )
            if message_id is None and turn_id is not None:
                assistant = (
                    session.execute(
                        select(ConversationMessageModel)
                        .where(
                            ConversationMessageModel.task_id == task_id,
                            ConversationMessageModel.turn_id == turn_id,
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
                turn_id,
                message_id,
                part_id,
            )
            result = self._record_change(
                session, task_id, "tool_call_created", "tool_call", call.id, turn_id
            )
            return call, result

    def transition_tool_call(
        self,
        task_id: int,
        tool_call_id: str,
        status: str,
        *,
        turn_id: int | None = None,
        fencing_version: int | None = None,
    ) -> ConversationMutationResult:
        """Atomically transition a tool call and publish the corresponding revision."""

        allowed = {
            "pending": {"running", "requires-action", "cancelled", "failed"},
            "requires-action": {"requires-action", "pending", "running", "cancelled", "failed"},
            "running": {"completed", "failed", "cancelled"},
        }
        valid = set(allowed) | {"completed", "failed", "cancelled"}
        if status not in valid:
            raise ValueError(f"unsupported tool call status: {status}")
        with self._write_session() as session:
            self._ensure_task(session, task_id)
            self._ensure_fencing(session, turn_id, fencing_version)
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
            return self._record_change(
                session, task_id, "tool_call_status_changed", "tool_call", call.id, turn_id
            )

    def complete_tool_call(
        self,
        task_id: int,
        call_id: int,
        result: object | None,
        status: str = "completed",
        error_text: str | None = None,
        turn_id: int | None = None,
        fencing_version: int | None = None,
    ) -> ConversationMutationResult:
        """Commit a tool-call terminal result and its change index atomically."""

        with self._write_session() as session:
            self._ensure_task(session, task_id)
            self._ensure_fencing(session, turn_id, fencing_version)
            call = self._tool_calls.complete_in_session(
                session,
                call_id,
                None if result is None else json.dumps(result, ensure_ascii=False, sort_keys=True),
                status,
                error_text,
            )
            return self._record_change(
                session, task_id, "tool_call_finished", "tool_call", call.id, turn_id
            )

    def complete_tool_call_by_external_id(
        self,
        task_id: int,
        tool_call_id: str,
        result: object | None,
        status: str = "completed",
        error_text: str | None = None,
        turn_id: int | None = None,
        fencing_version: int | None = None,
    ) -> ConversationMutationResult:
        """按模型调用 ID 原子收口工具调用事实。"""
        with self._write_session() as session:
            self._ensure_task(session, task_id)
            self._ensure_fencing(session, turn_id, fencing_version)
            existing = self._tool_calls.get_by_external_id_in_session(
                session, task_id, tool_call_id
            )
            call = self._tool_calls.complete_in_session(
                session,
                existing.id,
                None if result is None else json.dumps(result, ensure_ascii=False, sort_keys=True),
                status,
                error_text,
            )
            return self._record_change(
                session, task_id, "tool_call_finished", "tool_call", call.id, turn_id
            )

    @property
    def _messages_model(self):
        """Return the message ORM type without adding a second public abstraction."""

        from app.storage.model.conversation_message_model import ConversationMessageModel

        return ConversationMessageModel

    def _write_session(self) -> "_SessionContext":
        """Open an immediate SQLite write transaction for serialized revision allocation."""

        session = self._session_factory()
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        return _SessionContext(session)

    @staticmethod
    def _ensure_task(session: Session, task_id: int) -> None:
        """Raise a domain lookup error when the task aggregate does not exist."""

        if session.get(TaskModel, task_id) is None:
            raise KeyError(task_id)

    @staticmethod
    def _ensure_fencing(session: Session, turn_id: int | None, fencing_version: int | None) -> None:
        """校验可选的 run fencing version，拒绝过期 executor 写事实。"""
        if turn_id is None or fencing_version is None:
            return
        turn = session.get(TurnModel, turn_id)
        if turn is None:
            raise KeyError(turn_id)
        if turn.status not in {"pending", "running"} or turn.fencing_version != fencing_version:
            raise PermissionError(f"stale executor fencing version for run {turn_id}")

    def _next_message_sequence(self, session: Session, task_id: int) -> int:
        """Increment and return the task-local message sequence."""

        head = self._heads.get_or_create_in_session(session, task_id)
        head.message_sequence += 1
        session.flush()
        return head.message_sequence

    def _record_change(
        self,
        session: Session,
        task_id: int,
        change_type: str,
        entity_type: str,
        entity_id: int | None,
        turn_id: int | None,
    ) -> ConversationMutationResult:
        """Allocate the next task revision and append its committed change index."""

        head = self._heads.get_or_create_in_session(session, task_id)
        head.revision += 1
        session.flush()
        change = self._changes.create_in_session(
            session, task_id, head.revision, change_type, entity_type, entity_id, turn_id
        )
        return ConversationMutationResult(head.revision, change)


class _SessionContext:
    """Commit/rollback adapter for an already-started immediate session transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def __enter__(self) -> Session:
        return self._session

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if exc_type is None:
            self._session.commit()
        else:
            self._session.rollback()
        self._session.close()
