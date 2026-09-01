"""``conversation_heads`` 表的事务内 CRUD。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.conversation_head_record import ConversationHeadRecord
from app.storage.model.conversation_head_model import ConversationHeadModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import to_text, utc_now


class ConversationHeadCrud:
    """提供 task 会话版本头的读取和事务内初始化。"""

    def __init__(self) -> None:
        """绑定主库 session 工厂。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 主库存储尚未初始化。

        副作用:
            无；仅保存共享 session 工厂。
        """

        self._session_factory = main_session_factory()

    def get_by_task(self, task_id: int) -> ConversationHeadRecord | None:
        """查询 task 的会话版本头。

        参数:
            task_id: task 整数标识。

        返回:
            版本头记录；不存在时返回 ``None``。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 查询失败。

        副作用:
            打开一次只读 session。
        """

        with self._session_factory() as session:
            row = session.execute(
                select(ConversationHeadModel).where(ConversationHeadModel.task_id == task_id)
            ).scalar_one_or_none()
        return None if row is None else ConversationHeadRecord.from_model(row)

    @staticmethod
    def get_or_create_in_session(session: Session, task_id: int) -> ConversationHeadModel:
        """在调用方事务中取得或初始化 task 版本头。

        参数:
            session: 调用方持有的写事务 session。
            task_id: task 整数标识。

        返回:
            已加入当前 session 且可继续更新的 ORM 版本头。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 查询或插入失败。

        副作用:
            task 没有版本头时向 ``conversation_heads`` 插入初始 revision=0 行。
        """

        row = session.execute(
            select(ConversationHeadModel).where(ConversationHeadModel.task_id == task_id)
        ).scalar_one_or_none()
        if row is not None:
            return row
        now = to_text(utc_now())
        row = ConversationHeadModel(
            task_id=task_id,
            revision=0,
            message_sequence=0,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return row

    @staticmethod
    def delete_by_task_ids(session: Session, task_ids: set[int]) -> None:
        """在调用方事务中删除指定 task 的版本头。

        参数:
            session: 调用方持有的写事务 session。
            task_ids: 待删除的 task 标识集合。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 删除失败。

        副作用:
            删除对应的 ``conversation_heads`` 行；空集合不执行操作。
        """

        if task_ids:
            session.query(ConversationHeadModel).filter(
                ConversationHeadModel.task_id.in_(task_ids)
            ).delete(synchronize_session=False)
