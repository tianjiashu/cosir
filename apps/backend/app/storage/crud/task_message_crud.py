"""外部消息队列的存取；事务、投递与确认语义由 ``TaskMessageQueue`` 决定。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from pydantic import TypeAdapter
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models.task_message import Delivery, MessageBody, TaskMessage
from app.storage.model.task_message_model import TaskMessageModel

_BODY: TypeAdapter[MessageBody] = TypeAdapter(MessageBody)


class TaskMessageCrud:
    def append(
        self, session: Session, rows: Iterable[tuple[int, TaskMessage]]
    ) -> list[int]:
        """按顺序追加 ``(task_id, message)``，返回行 id（即消息的稳定身份）。"""

        models = [
            TaskMessageModel(
                task_id=task_id,
                kind=message.body.kind,
                payload=message.body.model_dump(mode="json"),
                delivery=message.delivery,
                project_to_transport=message.project_to_transport,
            )
            for task_id, message in rows
        ]
        session.add_all(models)
        session.flush()
        return [model.id for model in models]

    def list_pending(self, session: Session, task_id: int) -> list[TaskMessage]:
        """按入队顺序列出待并入消息；读取边界按 ``kind`` 校验 payload。"""

        statement = (
            select(TaskMessageModel)
            .where(TaskMessageModel.task_id == task_id)
            .order_by(TaskMessageModel.id)
        )
        return [_to_message(row) for row in session.scalars(statement)]

    def update_body_if_exists(
        self, session: Session, task_id: int, message_id: int, message: TaskMessage
    ) -> bool:
        """条件更新一条仍在队列中的消息正文；行已被确认删除时返回 ``False``。"""

        result = session.execute(
            update(TaskMessageModel)
            .where(TaskMessageModel.id == message_id, TaskMessageModel.task_id == task_id)
            .values(payload=message.body.model_dump(mode="json"))
        )
        return bool(result.rowcount)

    def delete_returning(self, session: Session, ids: list[int]) -> dict[int, TaskMessage]:
        """删除并返回实际删除的行（按 id）；已被删除的行不在结果里，供调用方识别重复确认。

        返回删除那一刻的行内容：排队期间被原地编辑过的消息以此为准，而不是投递时的副本。
        """

        if not ids:
            return {}
        result = session.scalars(
            delete(TaskMessageModel)
            .where(TaskMessageModel.id.in_(ids))
            .returning(TaskMessageModel)
        )
        return {row.id: _to_message(row) for row in result}


def _to_message(row: TaskMessageModel) -> TaskMessage:
    return TaskMessage(
        id=row.id,
        body=_BODY.validate_python(row.payload),
        delivery=cast(Delivery, row.delivery),
        project_to_transport=row.project_to_transport,
    )
