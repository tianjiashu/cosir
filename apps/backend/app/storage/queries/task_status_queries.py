"""Task 状态的只读投影：当前 Run 的状态（经当前 Run 指针）与「等你操作」的调用数。

不按创建时间猜测当前 Run。两项是正交的事实：Run 怎么走（唤醒、落定、停止）不影响有没有事在等你，
等你操作的事实源是 HITL 等待占位行（规则见 ``ConversationTaskContextCrud.awaiting_user``）。
"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
from app.storage.model.conversation_run_model import ConversationRunModel
from app.storage.model.conversation_task_context_model import ConversationTaskContextModel
from app.storage.model.task_model import TaskModel


@dataclass(frozen=True)
class TaskStatus:
    """一个 Task 的状态：执行状态（当前 Run 的 ``status``，无 Run 时为 None）与等你操作的调用数。"""

    execution_status: str | None
    awaiting_user_calls: int


class TaskStatusQueries:
    """两项在同一个读事务里读取，避免指针、状态与等待数来自不同读取时刻。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    _EXECUTION = select(TaskModel.id, ConversationRunModel.status).outerjoin(
        ConversationRunModel, TaskModel.current_run_id == ConversationRunModel.id
    )
    # 部分索引只收录 pending 行：整表对账只读当下在等的那几行。
    _AWAITING = (
        select(ConversationTaskContextModel.task_id, func.count())
        .where(ConversationTaskContextCrud.awaiting_user())
        .group_by(ConversationTaskContextModel.task_id)
    )

    def status(self, task_id: int) -> TaskStatus:
        """单个 Task 的状态；Task 不存在时抛 ``KeyError``。"""
        with self._session_factory() as session:
            row = session.execute(self._EXECUTION.where(TaskModel.id == task_id)).one_or_none()
            if row is None:
                raise KeyError(task_id)
            awaiting = session.execute(
                self._AWAITING.where(ConversationTaskContextModel.task_id == task_id)
            ).one_or_none()
        return TaskStatus(row.status, awaiting[1] if awaiting else 0)

    def list_statuses(self) -> dict[int, TaskStatus]:
        """全部 Task（含委派子任务）的状态，供状态推送的整表帧与周期对账。"""
        with self._session_factory() as session:
            executions = session.execute(self._EXECUTION).tuples().all()
            awaiting = dict(session.execute(self._AWAITING).tuples().all())
        return {
            task_id: TaskStatus(status, awaiting.get(task_id, 0)) for task_id, status in executions
        }
