"""输入 inbox 的存取；事务与消费语义由 service 决定。"""

from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models.conversation_input import InputDelivery, QueuedTaskInput
from app.storage.model.task_input_model import TaskInputModel


class TaskInputCrud:
    def append(
        self,
        session: Session,
        task_id: int,
        payload: QueuedTaskInput,
        delivery: InputDelivery = "immediate",
    ) -> int:
        """追加一条待消费输入，返回行 id（即该用户消息的稳定身份）。"""

        row = TaskInputModel(
            task_id=task_id, payload=payload.model_dump(mode="json"), delivery=delivery
        )
        session.add(row)
        session.flush()
        return row.id

    def list_pending(
        self,
        session: Session,
        task_id: int,
        delivery: InputDelivery | None = None,
    ) -> list[TaskInputModel]:
        """按入队顺序列出待消费输入；``delivery`` 非空时只列该投递方式的行。"""

        statement = select(TaskInputModel).where(TaskInputModel.task_id == task_id)
        if delivery is not None:
            statement = statement.where(TaskInputModel.delivery == delivery)
        return list(session.scalars(statement.order_by(TaskInputModel.id)))

    def release_all_immediate(self, session: Session, task_id: int) -> None:
        """把该 Task 全部待消费输入批量改为 ``immediate``；只改调度状态，不碰 payload。"""

        session.execute(
            update(TaskInputModel)
            .where(TaskInputModel.task_id == task_id, TaskInputModel.delivery != "immediate")
            .values(delivery="immediate")
        )

    def delete(self, session: Session, ids: list[int]) -> None:
        session.execute(delete(TaskInputModel).where(TaskInputModel.id.in_(ids)))
