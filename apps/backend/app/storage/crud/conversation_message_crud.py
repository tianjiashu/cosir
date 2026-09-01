"""``conversation_messages`` 表的事务内 CRUD。"""

from sqlalchemy import asc, select
from sqlalchemy.orm import Session

from app.models.conversation_message_record import ConversationMessageRecord
from app.storage.model.conversation_message_model import ConversationMessageModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import to_text, utc_now


class ConversationMessageCrud:
    """提供会话消息事实的写入和读取。"""

    def __init__(self) -> None:
        """绑定主库 session 工厂。"""

        self._session_factory = main_session_factory()

    @staticmethod
    def create_in_session(
        session: Session,
        task_id: int,
        sequence: int,
        role: str,
        turn_id: int | None = None,
        status: str = "complete",
        end_reason: str | None = None,
    ) -> ConversationMessageRecord:
        """在调用方事务中创建一条会话消息事实。

        参数:
            session: 调用方持有的写事务 session。
            task_id: task 整数标识。
            sequence: task 内稳定显示顺序。
            role: ``system``、``user``、``assistant`` 或 ``tool``。
            turn_id: 过渡 run/turn 标识，可为空。
            status: 消息状态，默认 ``complete``。
            end_reason: 终止原因，可为空。

        返回:
            已 flush 的消息记录。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 写入失败。

        副作用:
            插入 ``conversation_messages`` 行；事务提交由调用方负责。
        """

        now = to_text(utc_now())
        row = ConversationMessageModel(
            task_id=task_id,
            turn_id=turn_id,
            sequence=sequence,
            role=role,
            status=status,
            end_reason=end_reason,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return ConversationMessageRecord.from_model(row)

    def list_by_task(self, task_id: int) -> list[ConversationMessageRecord]:
        """按 task 读取全部消息事实。

        参数:
            task_id: task 整数标识。

        返回:
            按 task 内 sequence 升序的消息记录。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 查询失败。

        副作用:
            打开一次只读 session。
        """

        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(ConversationMessageModel)
                    .where(ConversationMessageModel.task_id == task_id)
                    .order_by(asc(ConversationMessageModel.sequence))
                )
                .scalars()
                .all()
            )
        return [ConversationMessageRecord.from_model(row) for row in rows]

    def delete_by_task_ids(self, session: Session, task_ids: set[int]) -> None:
        """在调用方事务中删除指定 task 的消息事实。"""

        if task_ids:
            session.query(ConversationMessageModel).filter(
                ConversationMessageModel.task_id.in_(task_ids)
            ).delete(synchronize_session=False)
