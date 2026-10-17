"""Task 与当前 Run 的只读投影；不写入状态，也不按创建时间猜测当前 Run。"""

from dataclasses import replace

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.task_record import TaskRecord
from app.storage.model.conversation_run_model import ConversationRunModel
from app.storage.model.task_model import TaskModel


class TaskReadQueries:
    """在一条 SQL 中读取 Task 与其指向的 Run，避免标识和状态来自不同读取时刻。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _statement() -> Select[tuple[TaskModel, str]]:
        return select(TaskModel, ConversationRunModel.status).outerjoin(
            ConversationRunModel, TaskModel.current_run_id == ConversationRunModel.id
        )

    def get(self, task_id: int) -> TaskRecord:
        """读取包括委派子任务在内的单个 Task；不存在时交由 API 将 KeyError 映射为 404。"""
        with self._session_factory() as session:
            row = session.execute(self._statement().where(TaskModel.id == task_id)).one_or_none()
            if row is None:
                raise KeyError(task_id)
            task, status = row
            return replace(TaskRecord.from_model(task), execution_status=status)

    def list_by_workspace(self, workspace_id: int) -> list[TaskRecord]:
        """批量投影侧栏的 user / fork 任务；委派子任务通过详情入口读取。"""
        with self._session_factory() as session:
            rows = session.execute(
                self._statement()
                .where(
                    TaskModel.workspace_id == workspace_id,
                    TaskModel.task_type.in_(("user", "fork")),
                )
                .order_by(TaskModel.updated_at.desc(), TaskModel.id.desc())
            )
            return [
                replace(TaskRecord.from_model(task), execution_status=status)
                for task, status in rows
            ]
