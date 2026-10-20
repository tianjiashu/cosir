"""输入 inbox 的存取；事务与消费语义由 service 决定。"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.conversation_input import QueuedTaskInput
from app.storage.model.task_input_model import TaskInputModel


class TaskInputCrud:
    def append(self, session: Session, task_id: int, payload: QueuedTaskInput) -> int:
        """追加一条待消费输入，返回行 id（即该用户消息的稳定身份）。"""

        row = TaskInputModel(task_id=task_id, payload=payload.model_dump(mode="json"))
        session.add(row)
        session.flush()
        return row.id

    def list_pending(self, session: Session, task_id: int) -> list[TaskInputModel]:
        return list(
            session.scalars(
                select(TaskInputModel)
                .where(TaskInputModel.task_id == task_id)
                .order_by(TaskInputModel.id)
            )
        )

    def replace(self, session: Session, row: TaskInputModel, payload: QueuedTaskInput) -> None:
        row.payload = payload.model_dump(mode="json")
        session.flush()

    def delete(self, session: Session, ids: list[int]) -> None:
        session.execute(delete(TaskInputModel).where(TaskInputModel.id.in_(ids)))
