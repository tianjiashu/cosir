"""conversation_commands 表的数据访问层。"""

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models.conversation_command_record import ConversationCommandRecord
from app.storage.model.conversation_command_model import ConversationCommandModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import to_text, utc_now


class ConversationCommandCrud:
    """提供 Transport 命令的持久化和状态更新。"""

    def __init__(self) -> None:
        """绑定主库 session 工厂。"""
        self._session_factory = main_session_factory()

    def get(self, task_id: int, command_id: str) -> ConversationCommandRecord | None:
        """查询任务内的命令记录。"""
        with self._session_factory() as session:
            row = session.execute(
                select(ConversationCommandModel).where(
                    ConversationCommandModel.task_id == task_id,
                    ConversationCommandModel.command_id == command_id,
                )
            ).scalar_one_or_none()
        return None if row is None else ConversationCommandRecord.from_model(row)

    def get_by_turn(self, turn_id: int) -> ConversationCommandRecord | None:
        """按运行标识读取绑定命令。"""
        with self._session_factory() as session:
            row = session.execute(
                select(ConversationCommandModel).where(ConversationCommandModel.turn_id == turn_id)
            ).scalar_one_or_none()
        return None if row is None else ConversationCommandRecord.from_model(row)

    @staticmethod
    def get_in_session(
        session: Session, task_id: int, command_id: str
    ) -> ConversationCommandRecord | None:
        """在调用方事务内读取任务命令映射。

        参数:
            session: 调用方持有的数据库事务会话。
            task_id: 任务标识。
            command_id: Transport 命令标识。

        返回:
            已存在的命令记录；不存在时返回 ``None``。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 查询失败。

        副作用:
            仅在当前事务内执行只读查询。
        """
        row = session.execute(
            select(ConversationCommandModel).where(
                ConversationCommandModel.task_id == task_id,
                ConversationCommandModel.command_id == command_id,
            )
        ).scalar_one_or_none()
        return None if row is None else ConversationCommandRecord.from_model(row)

    def reserve(
        self, task_id: int, command_id: str, command_type: str, payload_hash: str
    ) -> ConversationCommandRecord:
        """原子占用一个命令 ID；重复占用由数据库唯一索引拒绝。"""
        with self._session_factory.begin() as session:
            return self.reserve_in_session(session, task_id, command_id, command_type, payload_hash)

    @staticmethod
    def reserve_in_session(
        session: Session, task_id: int, command_id: str, command_type: str, payload_hash: str
    ) -> ConversationCommandRecord:
        """在调用方事务中创建命令映射并占用幂等键。

        参数:
            session: 调用方持有的数据库事务会话。
            task_id: 所属任务标识。
            command_id: Transport 命令标识。
            command_type: 命令类型。
            payload_hash: 命令业务载荷指纹。

        返回:
            已 flush 且拥有数据库主键的命令记录。

        异常:
            sqlalchemy.exc.IntegrityError: 并发请求抢先占用了相同幂等键。
            sqlalchemy.exc.SQLAlchemyError: 数据库写入失败。

        副作用:
            在当前事务中插入 ``conversation_commands`` 行；事务提交由调用方决定。
        """
        now = to_text(utc_now())
        row = ConversationCommandModel(
            task_id=task_id,
            command_id=command_id,
            command_type=command_type,
            payload_hash=payload_hash,
            status="processing",
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return ConversationCommandRecord.from_model(row)

    def bind_turn(self, record_id: int, turn_id: int) -> None:
        """把命令记录绑定到已创建的 turn。"""
        with self._session_factory.begin() as session:
            self.bind_turn_in_session(session, record_id, turn_id)

    @staticmethod
    def bind_turn_in_session(session: Session, record_id: int, turn_id: int) -> None:
        """在调用方事务中绑定命令与 Turn。

        参数:
            session: 调用方持有的数据库事务会话。
            record_id: 命令记录主键。
            turn_id: 新建 Turn 主键。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 数据库更新失败。

        副作用:
            在当前事务内写入命令的 ``turn_id`` 和更新时间。
        """
        result = session.execute(
            update(ConversationCommandModel)
            .where(ConversationCommandModel.id == record_id)
            .values(turn_id=turn_id, updated_at=to_text(utc_now()))
        )
        if result.rowcount != 1:
            raise KeyError(record_id)

    def mark_failed(self, record_id: int, error_code: str) -> None:
        """记录命令处理失败并保留幂等占用。

        参数:
            record_id: ``conversation_commands`` 行标识。
            error_code: 稳定的内部错误码，不包含密钥或用户输入。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 底层更新失败。

        副作用:
            将命令置为 ``failed``，并刷新更新时间。
        """
        with self._session_factory.begin() as session:
            session.execute(
                update(ConversationCommandModel)
                .where(ConversationCommandModel.id == record_id)
                .values(status="failed", error_code=error_code, updated_at=to_text(utc_now()))
            )

    def mark_status(self, record_id: int, status: str) -> None:
        """以受数据库约束保护的方式更新命令终态。

        参数:
            record_id: ``conversation_commands`` 行标识。
            status: ``completed``、``failed`` 或 ``cancelled`` 之一。

        返回:
            无。

        异常:
            ValueError: ``status`` 不是允许的命令终态。
            sqlalchemy.exc.SQLAlchemyError: 底层更新失败。

        副作用:
            更新命令状态和更新时间；不存在的行不产生写入。
        """
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError(f"unsupported conversation command status: {status}")
        with self._session_factory.begin() as session:
            session.execute(
                update(ConversationCommandModel)
                .where(ConversationCommandModel.id == record_id)
                .values(status=status, updated_at=to_text(utc_now()))
            )

    def delete_by_task_ids(self, task_ids: set[int]) -> None:
        """删除任务集合对应的命令记录。

        参数:
            task_ids: 要删除的任务标识集合。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 底层删除失败。

        副作用:
            删除指定任务下的全部命令映射；空集合不执行数据库操作。
        """
        if not task_ids:
            return
        with self._session_factory.begin() as session:
            session.execute(
                delete(ConversationCommandModel).where(
                    ConversationCommandModel.task_id.in_(task_ids)
                )
            )
