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

    def list_pending_deferred(
            self, task_id: int | None = None, session: Session | None = None
    ) -> list[ConversationTaskContextRecord]:
        """仍为 ``pending`` 的延迟结果后台占位行（tool 行 metadata ``background`` 为真）。

        ``task_id`` 为 ``None`` 时跨全部 Task（重启收尾用）。按 metadata JSON 过滤，不改表结构。
        """

        metadata_json = ConversationTaskContextModel.metadata_json
        stmt = select(ConversationTaskContextModel).where(
            func.json_extract(metadata_json, "$.background").is_(True),
            func.json_extract(metadata_json, "$.status") == "pending",
        )
        if task_id is not None:
            stmt = stmt.where(ConversationTaskContextModel.task_id == task_id)
        stmt = stmt.order_by(
            ConversationTaskContextModel.task_id.asc(), ConversationTaskContextModel.sequence.asc()
        )
        if session is not None:
            rows = session.scalars(stmt).all()
        else:
            with self._session_factory.begin() as session:
                rows = session.scalars(stmt).all()
        return [ConversationTaskContextRecord._from_model(row) for row in rows]

    def get_for_observation(
            self,
            task_id: int,
            run_id: int | None = None,
    ) -> list[ConversationTaskContextRecord]:
        """读取观测页所需的全量消息，不按上下文或流式标记过滤。

        指定 Run 时同时返回 Task 固定 system prompt（sequence 0），使 Run 视图保留
        任务级提示词；其它 Run 的消息不混入结果。时间戳由完整持久化记录映射保留。
        """

        statement = select(ConversationTaskContextModel).where(
            ConversationTaskContextModel.task_id == task_id
        )
        if run_id is not None:
            statement = statement.where(
                or_(
                    ConversationTaskContextModel.run_id == run_id,
                    and_(
                        ConversationTaskContextModel.run_id.is_(None),
                        ConversationTaskContextModel.sequence == 0,
                    ),
                )
            )
        statement = statement.order_by(ConversationTaskContextModel.sequence.asc())
        with self._session_factory.begin() as session:
            rows = session.scalars(statement).all()
        return [ConversationTaskContextRecord._from_model(row) for row in rows]

    def get_by_task_sequence(
            self,
            task_id: int,
            sequence: int,
            session: Session | None = None,
    ) -> ConversationTaskContextRecord | None:
        """按 Task 与全局序号读取唯一 context 行。

        参数:
            task_id: context 所属 Task。
            sequence: Task 范围内唯一的消息序号。
            session: 外部事务 Session；为 None 时自建只读事务。

        返回:
            匹配记录；不存在时返回 None。

        异常:
            sqlalchemy.exc.MultipleResultsFound: 若数据库唯一约束损坏导致出现重复行。

        副作用:
            无；只读取并反序列化一条记录。
        """

        statement = select(ConversationTaskContextModel).where(
            ConversationTaskContextModel.task_id == task_id,
            ConversationTaskContextModel.sequence == sequence,
        )
        if session is not None:
            row = session.scalars(statement).one_or_none()
        else:
            with self._session_factory.begin() as owned_session:
                row = owned_session.scalars(statement).one_or_none()
        return ConversationTaskContextRecord._from_model(row) if row is not None else None

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
    ) -> None:
        """写入一条上下文消息行。

        参数:
            record: 待持久化的记录（含消息事实、模型上下文标记、Transport 投影标记与 sequence）。
            session: 外部事务 Session；为 ``None`` 时自建事务并提交。

        返回:
            无。

        异常:
            sqlalchemy.exc.IntegrityError: 调用方分配的序号撞上 ``(task_id, sequence)`` 唯一约束，
                或其它数据库错误——一律向上抛出，不在此吞掉。

        副作用:
            传入 ``session`` 时仅 ``add`` 不提交（由调用方事务收口）；
            传入 ``None`` 时自建事务并在退出时提交。
        """

        model = record._to_model()
        if session is None:
            with self._session_factory.begin() as owned_session:
                self.create(record, session=owned_session)
            return
        # 外部事务是唯一提交边界；SQLite legacy 模式下首个 SAVEPOINT 的释放会提前提交。
        session.add(model)
        session.flush()

    def replace_message(
            self,
            task_id: int,
            sequence: int,
            record: ConversationTaskContextRecord,
            session: Session | None = None,
    ) -> None:
        """原子替换同一条 context 行的内容：消息正文、归属、模型与展示标记及完整角色 metadata。

        本方法是「原地替换一行」的唯一入口（占位被真实工具结果覆盖等操作都
        经它），因此**不改变行身份与顺序**：``id`` / ``task_id`` / ``sequence`` 保持原值，调用方
        必须保证 ``record`` 与目标 task/sequence 匹配。

        metadata 与正文整体替换，不保留旧等待请求或错误；旁路状态更新使用下方的原子 JSON 更新。

        参数:
            task_id: 目标行所属 Task。
            sequence: 目标行的序号（行身份）。
            record: 覆盖用的新内容；其 ``task_id`` / ``sequence`` 必须与入参一致。
            session: 外部事务 Session；为 ``None`` 时自建事务并提交。

        异常:
            ValueError: ``record`` 的 task/sequence 与目标不匹配。
            KeyError: 目标行不存在。

        副作用:
            就地更新该行的 ``run_id`` / ``message_json`` / ``include_in_context`` /
            ``project_to_transport`` 与完整 ``metadata_json``；不新增、不删除行。
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
        model.message_json = replacement.message_json
        model.metadata_json = replacement.metadata_json
        model.include_in_context = replacement.include_in_context
        model.project_to_transport = replacement.project_to_transport
        session.flush()

    def update_child_display_status(
            self,
            task_id: int,
            sequence: int,
            child_task_id: int,
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
            status: 子任务最新 Run 的 canonical 生命周期状态。
            session: 可选外部事务。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 数据库写入失败。

        副作用:
            仅在 locator 仍匹配时更新 ``metadata_json``；不发布 Assistant
            Transport 事件。
        """

        if session is None:
            with self._session_factory.begin() as owned_session:
                self.update_child_display_status(
                    task_id,
                    sequence,
                    child_task_id,
                    status,
                    session=owned_session,
                )
            return

        metadata_json = ConversationTaskContextModel.metadata_json
        display_kind = func.json_extract(metadata_json, "$.display_data.kind")
        statement = (
            update(ConversationTaskContextModel)
            .where(
                ConversationTaskContextModel.task_id == task_id,
                ConversationTaskContextModel.sequence == sequence,
                func.json_extract(metadata_json, "$.display_data.child_task_id") == child_task_id,
                or_(
                    display_kind == "delegation-result",
                    and_(
                        display_kind == "child-agent-result",
                        func.json_extract(metadata_json, "$.display_data.operation") == "send",
                    ),
                ),
            )
            .values(
                metadata_json=func.json_set(
                    metadata_json,
                    "$.display_data.status",
                    status,
                )
            )
        )
        session.execute(statement)

    def exclude_from_sequence(self, task_id: int, sequence: int, session: Session) -> None:
        """把 ``sequence`` 及其后的行移出模型上下文与前端投影；行本身保留，供观测页查看。

        由编辑用例决定位置。行不删除，所以新消息必须追加在 :meth:`max_sequence` 之后，不得复用
        这里任何行的序号。
        """
        session.execute(
            update(ConversationTaskContextModel)
            .where(
                ConversationTaskContextModel.task_id == task_id,
                ConversationTaskContextModel.sequence >= sequence,
            )
            .values(include_in_context=False, project_to_transport=False)
        )

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
