"""Task 执行状态的只读投影：经当前 Run 指针读 ``ConversationRunModel.status``。

不按创建时间猜测当前 Run。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.storage.model.conversation_run_model import ConversationRunModel
from app.storage.model.task_model import TaskModel


class TaskStatusQueries:
    """Task 与其当前 Run 在一条 SQL 里外连接读取，避免指针与状态来自不同读取时刻。

    无当前 Run 时状态为 None。
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    _STATEMENT = select(TaskModel.id, ConversationRunModel.status).outerjoin(
        ConversationRunModel, TaskModel.current_run_id == ConversationRunModel.id
    )

    def status(self, task_id: int) -> str | None:
        """单个 Task 的执行状态；Task 不存在时抛 ``KeyError``。"""
        with self._session_factory() as session:
            row = session.execute(self._STATEMENT.where(TaskModel.id == task_id)).one_or_none()
        if row is None:
            raise KeyError(task_id)
        return row.status

    def list_statuses(self) -> dict[int, str | None]:
        """全部 Task（含委派子任务与 Team 节点）的执行状态，供状态推送的整表帧与周期对账。"""
        with self._session_factory() as session:
            return dict(session.execute(self._STATEMENT).tuples().all())
