"""``conversation_tool_calls`` 表的事务内 CRUD。"""

from sqlalchemy import asc, select
from sqlalchemy.orm import Session

from app.models.conversation_tool_call_record import ConversationToolCallRecord
from app.storage.model.conversation_tool_call_model import ConversationToolCallModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import to_text, utc_now


class ConversationToolCallCrud:
    """提供结构化工具调用事实的创建、读取和条件收尾。"""

    def __init__(self) -> None:
        """绑定主库 session 工厂。"""

        self._session_factory = main_session_factory()

    @staticmethod
    def create_in_session(
        session: Session,
        task_id: int,
        tool_call_id: str,
        tool_name: str,
        args_json: str,
        run_id: int | None = None,
        message_id: int | None = None,
        part_id: int | None = None,
        status: str = "pending",
    ) -> ConversationToolCallRecord:
        """在调用方事务中创建工具调用事实。

        参数:
            session: 调用方持有的写事务 session。
            task_id: task 整数标识。
            tool_call_id: 工具调用幂等标识，在 task 内唯一。
            tool_name: 工具名称。
            args_json: 已规范化的参数 JSON 文本。
            run_id: 过渡 run/turn 标识，可为空。
            message_id: 关联的 assistant 消息，可为空。
            part_id: 关联的 tool-call part，可为空。
            status: 初始状态，默认 ``pending``。

        返回:
            已 flush 的工具调用记录。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 写入失败。

        副作用:
            插入 ``conversation_tool_calls`` 行；事务提交由调用方负责。
        """

        now = to_text(utc_now())
        row = ConversationToolCallModel(
            task_id=task_id,
            run_id=run_id,
            message_id=message_id,
            part_id=part_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args_json=args_json,
            status=status,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return ConversationToolCallRecord.from_model(row)

    @staticmethod
    def complete_in_session(
        session: Session,
        tool_call_id: int,
        result_json: str | None,
        status: str = "completed",
        error_text: str | None = None,
    ) -> ConversationToolCallRecord:
        """在调用方事务中以单次写入落定工具调用结果。

        参数:
            session: 调用方持有的写事务 session。
            tool_call_id: 数据库工具调用主键。
            result_json: 最终结果 JSON 文本，可为空。
            status: 目标状态，默认 ``completed``。
            error_text: 可选的稳定错误描述。

        返回:
            更新后的工具调用事实记录。

        异常:
            KeyError: 工具调用不存在。
            ValueError: ``status`` 不是终态。
            sqlalchemy.exc.SQLAlchemyError: 更新失败。

        副作用:
            更新结果、状态、错误和更新时间；事务提交由调用方负责。
        """

        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError(f"unsupported tool call terminal status: {status}")
        row = session.get(ConversationToolCallModel, tool_call_id)
        if row is None:
            raise KeyError(tool_call_id)
        if row.status in {"completed", "failed", "cancelled"}:
            return ConversationToolCallRecord.from_model(row)
        if status == "completed" and row.status != "running":
            raise ValueError(f"tool call must be running before completion: {row.tool_call_id}")
        row.result_json = result_json
        row.status = status
        row.error_text = error_text
        row.updated_at = to_text(utc_now())
        session.flush()
        return ConversationToolCallRecord.from_model(row)

    def list_by_task(self, task_id: int) -> list[ConversationToolCallRecord]:
        """按 task 读取工具调用事实，按创建时间和主键升序返回。"""

        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(ConversationToolCallModel)
                    .where(ConversationToolCallModel.task_id == task_id)
                    .order_by(
                        asc(ConversationToolCallModel.created_at), asc(ConversationToolCallModel.id)
                    )
                )
                .scalars()
                .all()
            )
        return [ConversationToolCallRecord.from_model(row) for row in rows]

    @staticmethod
    def get_by_external_id_in_session(
        session: Session, task_id: int, tool_call_id: str
    ) -> ConversationToolCallRecord:
        """在当前事务中按 task 和模型提供的调用 ID 查找工具调用事实。"""
        row = (
            session.execute(
                select(ConversationToolCallModel).where(
                    ConversationToolCallModel.task_id == task_id,
                    ConversationToolCallModel.tool_call_id == tool_call_id,
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            raise KeyError(tool_call_id)
        return ConversationToolCallRecord.from_model(row)

    @staticmethod
    def get_by_external_id_model_in_session(
        session: Session, task_id: int, tool_call_id: str
    ) -> ConversationToolCallModel:
        """在当前事务中按 task 和模型调用 ID 返回可更新的 ORM 行。"""

        row = (
            session.execute(
                select(ConversationToolCallModel).where(
                    ConversationToolCallModel.task_id == task_id,
                    ConversationToolCallModel.tool_call_id == tool_call_id,
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            raise KeyError(tool_call_id)
        return row

    def delete_by_task_ids(self, session: Session, task_ids: set[int]) -> None:
        """在调用方事务中删除指定 task 的工具调用事实。"""

        if task_ids:
            session.query(ConversationToolCallModel).filter(
                ConversationToolCallModel.task_id.in_(task_ids)
            ).delete(synchronize_session=False)
