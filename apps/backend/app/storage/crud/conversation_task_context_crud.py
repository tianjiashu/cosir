"""Task context 消息行的行级事务内 CRUD。"""

from __future__ import annotations

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.models.conversation_task_context import ConversationTaskContextRecord
from app.storage.model.conversation_task_context_model import ConversationTaskContextModel
from app.storage.store_engines import main_session_factory


class ConversationTaskContextCrud:
    """只负责 ``conversation_task_contexts`` 表的行级读写。

    所有行↔对象映射统一经 :meth:`ConversationTaskContextRecord._from_model` /
    :meth:`ConversationTaskContextRecord._to_model`，不存在第二套序列化。
    """

    def __init__(self) -> None:
        """绑定共享主库 session 工厂。"""

        self._session_factory = main_session_factory()

    def get(
        self,
        task_id: int,
        include_in_context: bool = True,
        session: Session | None = None,
    ) -> list[ConversationTaskContextRecord]:
        """读取指定 task 的上下文消息行，按 ``sequence`` 升序返回。

        参数:
            task_id: 归属的 Task 标识。
            include_in_context: 为 ``True`` 时仅返回纳入上下文的行；为 ``False`` 时返回全部行。
            session: 外部事务 Session；为 ``None`` 时自建只读事务。

        返回:
            记录列表；无匹配时返回空列表（不返回 ``None``）。
        """

        stmt = select(ConversationTaskContextModel).where(
            ConversationTaskContextModel.task_id == task_id
        )
        if include_in_context:
            stmt = stmt.where(ConversationTaskContextModel.include_in_context.is_(True))
        stmt = stmt.order_by(ConversationTaskContextModel.sequence.asc())
        if session is not None:
            rows = session.scalars(stmt).all()
        else:
            with self._session_factory.begin() as session:
                rows = session.scalars(stmt).all()
        return [ConversationTaskContextRecord._from_model(row) for row in rows]

    def max_sequence(self, task_id: int, session: Session | None = None) -> int:
        """返回指定 task 下 ``sequence`` 列的最大值；无行时返回 0。

        参数:
            task_id: 归属的 Task 标识。

        返回:
            该 task 下 ``sequence`` 最大值（无行时为 0），可直接作为下一个序号种子。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 查询失败时抛出。
        """

        statement = select(func.coalesce(func.max(ConversationTaskContextModel.sequence), 0)).where(
            ConversationTaskContextModel.task_id == task_id
        )
        if session is not None:
            current = session.scalar(statement)
        else:
            with self._session_factory.begin() as owned_session:
                current = owned_session.scalar(statement)
        return int(current or 0)

    def create(
        self,
        record: ConversationTaskContextRecord,
        session: Session | None = None,
    ) -> bool:
        """写入一条上下文消息行。

        参数:
            record: 待持久化的记录（含 task_id、run_id、message、include_in_context、sequence）。
            session: 外部事务 Session；为 ``None`` 时自建事务并提交。

        返回:
            ``True`` 表示新增；违反工具调用唯一性时返回 ``False``。其他数据库错误继续抛出。

        副作用:
            传入 ``session`` 时仅 ``add`` 不提交（由调用方事务收口）；
            传入 ``None`` 时自建事务并在退出时提交。
        """

        model = record._to_model()
        if session is None:
            with self._session_factory.begin() as owned_session:
                return self.create(record, session=owned_session)
        with session.begin_nested():
            session.add(model)
            session.flush()
        return True

    def replace_message(
        self,
        task_id: int,
        sequence: int,
        record: ConversationTaskContextRecord,
        session: Session | None = None,
    ) -> None:
        """原子替换同一条 context 行的消息正文及流式标记。

        该方法只服务于稳定 sequence 的流式 assistant 草稿更新，不改变行身份、顺序或
        归属。调用方必须保证 ``record`` 与目标 task/sequence 匹配。
        """

        if record.task_id != task_id or record.sequence != sequence:
            raise ValueError("context replacement target does not match record")
        statement = select(ConversationTaskContextModel).where(
            ConversationTaskContextModel.task_id == task_id,
            ConversationTaskContextModel.sequence == sequence,
        )
        if session is None:
            with self._session_factory.begin() as owned_session:
                self.replace_message(task_id, sequence, record, session=owned_session)
            return
        model = session.scalar(statement)
        if model is None:
            raise KeyError(f"context row {task_id}/{sequence} not found")
        replacement = record._to_model()
        model.run_id = replacement.run_id
        model.tool_call_id = replacement.tool_call_id
        model.message_json = replacement.message_json
        model.include_in_context = replacement.include_in_context
        model.is_streaming = replacement.is_streaming
        session.flush()

    def update_child_display_status(
        self,
        task_id: int,
        sequence: int,
        child_task_id: int,
        child_run_id: int,
        status: str,
        session: Session | None = None,
    ) -> None:
        """原子更新一条动态 child display 的生命周期状态。

        该方法使用 SQLite JSON 函数只改 ``display_data.status``，避免把调用方读取到的
        完整 metadata 再写回而覆盖并发产生的其它 Transport 字段。locator 也放在 SQL
        条件中，因此 context 行在读取和更新之间被替换时不会误写新内容。

        参数:
            task_id: context 所属任务。
            sequence: 任务内 context 行的稳定序号；仅用于缩小更新目标。
            child_task_id: display_data 中的子任务标识。
            child_run_id: display_data 中的子 Run 标识。
            status: canonical child Run 生命周期状态。
            session: 可选外部事务。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 数据库写入失败。

        副作用:
            仅在 locator 仍匹配时更新 ``transport_metadata_json``；不发布 Assistant
            Transport 事件。
        """

        if session is None:
            with self._session_factory.begin() as owned_session:
                self.update_child_display_status(
                    task_id,
                    sequence,
                    child_task_id,
                    child_run_id,
                    status,
                    session=owned_session,
                )
            return

        metadata_json = ConversationTaskContextModel.transport_metadata_json
        display_kind = func.json_extract(metadata_json, "$.display_data.kind")
        statement = (
            update(ConversationTaskContextModel)
            .where(
                ConversationTaskContextModel.task_id == task_id,
                ConversationTaskContextModel.sequence == sequence,
                func.json_extract(metadata_json, "$.display_data.child_task_id")
                == child_task_id,
                func.json_extract(metadata_json, "$.display_data.child_run_id")
                == child_run_id,
                or_(
                    display_kind == "delegation-result",
                    and_(
                        display_kind == "child-agent-result",
                        func.json_extract(metadata_json, "$.display_data.operation") == "send",
                    ),
                ),
            )
            .values(
                transport_metadata_json=func.json_set(
                    metadata_json,
                    "$.display_data.status",
                    status,
                )
            )
        )
        session.execute(statement)

    def update_terminal_session_display(
        self,
        task_id: int,
        sequence: int,
        run_id: int,
        session_id: str,
        status: str,
        end_reason: str | None,
        exit_code: int | None,
        session: Session | None = None,
    ) -> None:
        """原子更新精确 terminal session display 的生命周期字段。

        locator 同时约束 task、context sequence、run 和 session_id，只修改已有的
        ``terminal-session`` display_data，避免异步 PTY 状态回写覆盖并发产生的其它 metadata。
        """

        if session is None:
            with self._session_factory.begin() as owned_session:
                self.update_terminal_session_display(
                    task_id,
                    sequence,
                    run_id,
                    session_id,
                    status,
                    end_reason,
                    exit_code,
                    session=owned_session,
                )
            return

        metadata_json = ConversationTaskContextModel.transport_metadata_json
        display_kind = func.json_extract(metadata_json, "$.display_data.kind")
        display_status = func.json_extract(metadata_json, "$.display_data.status")
        updated_metadata = func.json_set(
            metadata_json,
            "$.display_data.status",
            status,
            "$.display_data.end_reason",
            end_reason,
            "$.display_data.exit_code",
            exit_code,
        )
        statement = (
            update(ConversationTaskContextModel)
            .where(
                ConversationTaskContextModel.task_id == task_id,
                ConversationTaskContextModel.sequence == sequence,
                ConversationTaskContextModel.run_id == run_id,
                display_kind == "terminal-session",
                func.json_extract(metadata_json, "$.display_data.session_id") == session_id,
                # Session lifecycle is monotonic: only an active display may receive a
                # transition. A delayed terminal event must not overwrite an established
                # exited/failed/closed state.
                display_status.in_(("starting", "running")),
            )
            .values(transport_metadata_json=updated_metadata)
        )
        session.execute(statement)

    def delete_by_run_id(self, task_id: int, run_id: int, session: Session | None = None) -> None:
        """删除指定 task 下某 run 的全部上下文消息行。

        参数:
            task_id: 归属的 Task 标识。
            run_id: 待删除的 Conversation Run 标识。
            session: 外部事务 Session；为 ``None`` 时自建事务并提交。

        副作用:
            传入 ``session`` 时仅执行 DELETE 不提交（由调用方事务收口）；
            传入 ``None`` 时自建事务并在退出时提交。
        """

        stmt = select(ConversationTaskContextModel).where(
            ConversationTaskContextModel.task_id == task_id,
            ConversationTaskContextModel.run_id == run_id,
        )
        if session is None:
            with self._session_factory.begin() as session:
                for row in session.scalars(stmt).all():
                    session.delete(row)
            return
        for row in session.scalars(stmt).all():
            session.delete(row)

    def delete_by_task_ids(self, task_ids: list[int], session: Session | None = None) -> None:
        """按一批 task 的标识批量删除其全部上下文消息行。

        参数:
            task_ids: 待清理的 task 整数 id 列表。
            session: 可选外部事务 session；传入时复用该事务不自行提交，为 None 时
                自开事务并自动提交。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            从 ``conversation_task_contexts`` 表删除 ``task_id`` 命中的行；task_ids 为空
            或对应行不存在时静默无操作。
        """

        if not task_ids:
            return
        if session is not None:
            session.execute(
                delete(ConversationTaskContextModel).where(
                    ConversationTaskContextModel.task_id.in_(task_ids)
                )
            )
            return
        with self._session_factory.begin() as session:
            session.execute(
                delete(ConversationTaskContextModel).where(
                    ConversationTaskContextModel.task_id.in_(task_ids)
                )
            )
