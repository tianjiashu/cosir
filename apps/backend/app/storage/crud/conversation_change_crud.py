"""``conversation_changes`` 表的事务内 CRUD。"""

from sqlalchemy import asc, select
from sqlalchemy.orm import Session

from app.models.conversation_change_record import ConversationChangeRecord
from app.storage.model.conversation_change_model import ConversationChangeModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import to_text, utc_now


class ConversationChangeCrud:
    """提供已提交会话事实变更索引的写入和按 revision 查询。"""

    def __init__(self) -> None:
        """绑定主库 session 工厂。"""

        self._session_factory = main_session_factory()

    @staticmethod
    def create_in_session(
        session: Session,
        task_id: int,
        revision: int,
        change_type: str,
        entity_type: str,
        entity_id: int | None = None,
        turn_id: int | None = None,
    ) -> ConversationChangeRecord:
        """在调用方事务中记录一次事实变更索引。

        参数:
            session: 调用方持有的写事务 session。
            task_id: task 整数标识。
            revision: 已由版本头分配的 task 级 revision。
            change_type: 稳定的变更类型，例如 ``message_created``。
            entity_type: 受影响实体类型，例如 ``message``。
            entity_id: 受影响实体主键，可为空表示 task 级变更。
            turn_id: 关联的过渡 run/turn 标识，可为空。

        返回:
            已 flush 的变更记录。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 写入失败。

        副作用:
            向 ``conversation_changes`` 插入一行；事务提交由调用方负责。
        """

        now = to_text(utc_now())
        row = ConversationChangeModel(
            task_id=task_id,
            revision=revision,
            change_type=change_type,
            entity_type=entity_type,
            entity_id=entity_id,
            turn_id=turn_id,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return ConversationChangeRecord.from_model(row)

    def list_after(self, task_id: int, after_revision: int) -> list[ConversationChangeRecord]:
        """查询 task 中 revision 大于指定值的变更索引。

        参数:
            task_id: task 整数标识。
            after_revision: 不包含的最后已确认 revision。

        返回:
            按 revision 升序排列的变更记录；没有新变更时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 查询失败。

        副作用:
            打开一次只读 session。
        """

        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(ConversationChangeModel)
                    .where(
                        ConversationChangeModel.task_id == task_id,
                        ConversationChangeModel.revision > after_revision,
                    )
                    .order_by(asc(ConversationChangeModel.revision))
                )
                .scalars()
                .all()
            )
        return [ConversationChangeRecord.from_model(row) for row in rows]

    def delete_by_task_ids(self, session: Session, task_ids: set[int]) -> None:
        """在调用方事务中删除指定 task 的变更索引。

        参数:
            session: 调用方持有的写事务 session。
            task_ids: 待删除的 task 标识集合。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 删除失败。

        副作用:
            删除对应的 ``conversation_changes`` 行。
        """

        if task_ids:
            session.query(ConversationChangeModel).filter(
                ConversationChangeModel.task_id.in_(task_ids)
            ).delete(synchronize_session=False)
