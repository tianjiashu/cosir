"""Task context 消息行的行级事务内 CRUD。"""

from __future__ import annotations

from sqlalchemy import func, select
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

    def max_sequence(self, task_id: int) -> int:
        """返回指定 task 下 ``sequence`` 列的最大值；无行时返回 0。

        参数:
            task_id: 归属的 Task 标识。

        返回:
            该 task 下 ``sequence`` 最大值（无行时为 0），可直接作为下一个序号种子。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 查询失败时抛出。
        """

        with self._session_factory.begin() as session:
            current = session.scalar(
                select(
                    func.coalesce(func.max(ConversationTaskContextModel.sequence), 0)
                ).where(ConversationTaskContextModel.task_id == task_id)
            )
        return int(current or 0)

    def create(
        self,
        record: ConversationTaskContextRecord,
        session: Session | None = None,
    ) -> None:
        """写入一条上下文消息行。

        参数:
            record: 待持久化的记录（含 task_id、run_id、message、include_in_context、sequence）。
            session: 外部事务 Session；为 ``None`` 时自建事务并提交。

        副作用:
            传入 ``session`` 时仅 ``add`` 不提交（由调用方事务收口）；
            传入 ``None`` 时自建事务并在退出时提交。
        """

        model = record._to_model()
        if session is None:
            with self._session_factory.begin() as session:
                session.add(model)
                session.flush()
            return
        session.add(model)
        session.flush()

    def delete_by_run_id(
        self, task_id: int, run_id: int, session: Session | None = None
    ) -> None:
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
