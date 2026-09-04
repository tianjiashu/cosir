"""conversation_commands 表的数据访问层。"""

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models.conversation_command_record import ConversationCommandRecord
from app.storage.model.conversation_command_model import ConversationCommandModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import to_text, utc_now


class CommandDuplicateError(ValueError):
    """相同 command_id 与相同 payload 已提交，禁止重复创建运行。"""


class CommandPayloadConflictError(ValueError):
    """相同 command_id 但 payload 不同，提交冲突拒绝。"""


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

    def get_by_run(self, run_id: int) -> ConversationCommandRecord | None:
        """按运行标识读取绑定命令。"""
        with self._session_factory() as session:
            row = session.execute(
                select(ConversationCommandModel).where(ConversationCommandModel.run_id == run_id)
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

    def create(
        self,
        task_id: int,
        command_id: str,
        command_type: str,
        payload_hash: str,
        run_id: int | None = None,
        session: Session | None = None,
    ) -> ConversationCommandRecord:
        """原子占用一个命令 ID；重复占用由数据库唯一索引拒绝。

        主键 ``id`` 由存储引擎自增分配；``created_at`` / ``updated_at`` 由模型默认值统一
        填充。幂等键（``task_id`` + ``command_id``）的并发抢占由数据库唯一索引拒绝。
        ``run_id`` 在占用阶段通常尚不存在（运行随后才创建），故允许为空；后续由
        ``bind_run`` 或原子占用接口回填。

        ``session`` 为 ``None`` 时本方法自开事务并自动提交；传入外部会话时复用调用方事务，
        由调用方负责提交。这与 ``TaskCrud.create`` 的双形态约定保持一致，便于上层在单事务内
        编排「占用命令 → 其他写入」等操作。

        参数:
            task_id: 所属任务标识。
            command_id: Transport 命令标识（与 task_id 构成幂等键）。
            command_type: 命令类型。
            payload_hash: 命令业务载荷指纹。
            run_id: 关联的 Run 标识；占用阶段可为 ``None``，随后回填。
            session: 可选的外部 SQLAlchemy 会话。传入时复用该事务（调用方负责提交）；
                为 None 时由本方法自开事务并自动提交。

        返回:
            已 flush 且拥有数据库主键的命令记录。

        异常:
            sqlalchemy.exc.IntegrityError: 并发请求抢先占用了相同幂等键。
            sqlalchemy.exc.SQLAlchemyError: 数据库写入失败。

        副作用:
            向 ``conversation_commands`` 表插入一行；当 ``session`` 为 None 时由本方法提交事务。
        """
        if session is None:
            with self._session_factory.begin() as session:
                return self._do_create(
                    session, task_id, command_id, run_id, command_type, payload_hash
                )
        return self._do_create(session, task_id, command_id, run_id, command_type, payload_hash)

    def _do_create(
        self,
        session: Session,
        task_id: int,
        command_id: str,
        run_id: int | None,
        command_type: str,
        payload_hash: str,
    ) -> ConversationCommandRecord:
        """在给定会话中构造并 flush 一条命令记录。

        参数:
            session: 目标 SQLAlchemy 会话（已开启的事务）。
            task_id: 所属任务标识。
            command_id: Transport 命令标识。
            command_type: 命令类型。
            payload_hash: 命令业务载荷指纹。

        返回:
            已 flush 的 ``ConversationCommandRecord``。

        异常:
            sqlalchemy.exc.IntegrityError: 并发请求抢先占用了相同幂等键。
            sqlalchemy.exc.SQLAlchemyError: 数据库写入失败。

        副作用:
            在当前事务中插入一行命令；事务提交由调用方或 ``reserve`` 决定。
        """
        row = ConversationCommandModel(
            task_id=task_id,
            command_id=command_id,
            command_type=command_type,
            payload_hash=payload_hash,
            run_id=run_id,
        )
        session.add(row)
        session.flush()
        return ConversationCommandRecord.from_model(row)

    def mark_failed(self, record_id: int, error_code: str) -> None:
        """记录命令处理失败并保留幂等占用。

        ``conversation_commands`` 表只承载幂等占用职责，不持有运行态；故本方法
        仅持久化稳定的内部 ``error_code`` 供诊断与重试决策，不写入任何运行状态。

        参数:
            record_id: ``conversation_commands`` 行标识。
            error_code: 稳定的内部错误码，不包含密钥或用户输入。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 底层更新失败。

        副作用:
            更新命令的 ``error_code`` 并刷新更新时间；不存在的行不产生写入。
        """
        with self._session_factory.begin() as session:
            session.execute(
                update(ConversationCommandModel)
                .where(ConversationCommandModel.id == record_id)
                .values(error_code=error_code, updated_at=to_text(utc_now()))
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
