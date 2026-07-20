"""SQLite CRUD for the ``tasks`` table.

单一职责：提供 ``tasks`` 表的纯单表 CRUD 操作。
"""
from typing import List
from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from app.storage.model.task_model import TaskModel
from app.models import TaskRecord
from app.utils.datetime_utils import from_text, to_text, utc_now


class TaskCrud:
    """Pure CRUD for the ``tasks`` table."""

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def create(
        self,
        task_id: str,
        workspace_id: str,
        agent_id: str,
        input_text: str,
        title: str,
        last_message_preview: str,
        latest_turn_id: str,
        status: str,
    ) -> TaskRecord:
        now = utc_now()
        task = TaskRecord(
            task_id=task_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            input_text=input_text,
            title=title,
            last_message_preview=last_message_preview,
            latest_turn_id=latest_turn_id,
            status=status,
            created_at=now,
            updated_at=now,
        )
        with self._session_factory.begin() as session:
            session.add(
                TaskModel(
                    task_id=task.task_id,
                    workspace_id=task.workspace_id,
                    agent_id=task.agent_id,
                    input_text=task.input_text,
                    title=task.title,
                    last_message_preview=task.last_message_preview,
                    latest_turn_id=task.latest_turn_id,
                    status=task.status,
                    created_at=to_text(task.created_at),
                    updated_at=to_text(task.updated_at),
                )
            )
        return task

    def list_by_workspace(self, workspace_id: str) -> List[TaskRecord]:
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(TaskModel)
                    .where(TaskModel.workspace_id == workspace_id)
                    .order_by(TaskModel.updated_at.desc(), TaskModel.task_id.desc())
                )
                .scalars()
                .all()
            )
        return [self._task_from_model(row) for row in rows]

    def get(self, task_id: str) -> TaskRecord:
        with self._session_factory() as session:
            row = session.get(TaskModel, task_id)
        if row is None:
            raise KeyError(task_id)
        return self._task_from_model(row)

    def update_status(self, task_id: str, status: str) -> TaskRecord:
        self.get(task_id)
        with self._session_factory.begin() as session:
            session.execute(
                update(TaskModel)
                .where(TaskModel.task_id == task_id)
                .values(status=status, updated_at=to_text(utc_now()))
            )
        return self.get(task_id)

    def has_status(self, task_id: str, status: str) -> bool:
        return self.get(task_id).status == status

    def update_latest_turn(self, task_id: str, latest_turn_id: str, last_message_preview: str) -> None:
        with self._session_factory.begin() as session:
            session.execute(
                update(TaskModel)
                .where(TaskModel.task_id == task_id)
                .values(
                    latest_turn_id=latest_turn_id,
                    last_message_preview=last_message_preview,
                    updated_at=to_text(utc_now()),
                )
            )

    def list_ids_by_workspace(self, workspace_id: str) -> list[str]:
        from sqlalchemy import select
        with self._session_factory() as session:
            return [
                row[0]
                for row in session.execute(
                    select(TaskModel.task_id).where(TaskModel.workspace_id == workspace_id)
                ).all()
            ]

    def delete_by_ids(self, task_ids: list[str]) -> None:
        from sqlalchemy import delete
        with self._session_factory.begin() as session:
            session.execute(delete(TaskModel).where(TaskModel.task_id.in_(task_ids)))

    def _task_from_model(self,row: TaskModel) -> TaskRecord:
        """Convert a task ORM model to a domain record."""
        return TaskRecord(
            task_id=row.task_id,
            workspace_id=row.workspace_id,
            agent_id=row.agent_id,
            input_text=row.input_text,
            title=row.title,
            last_message_preview=row.last_message_preview,
            latest_turn_id=row.latest_turn_id,
            status=row.status,
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
        )
