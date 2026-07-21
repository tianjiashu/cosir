"""SQLite turn CRUD — pure data access for the ``turns`` table.

单一职责：提供 ``turns`` 表的纯 CRUD 操作。

职责边界：
- 负责：turn 单表读写、model↔record 转换。
- 不负责：跨表操作（由 ``service/task/`` 编排）。
"""

from typing import List
from uuid import uuid4

from sqlalchemy import asc, select, update

from app.storage.model.turn_model import TurnModel
from app.models import TurnRecord
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import from_text, to_text, utc_now


class TurnCrud:
    """Pure CRUD for the ``turns`` table."""

    def __init__(self) -> None:
        """Initialize with the shared main-database session factory."""
        self._session_factory = main_session_factory()

    def create(self, task_id: str, input_text: str, status: str = "pending") -> TurnRecord:
        """Create a turn record."""

        if not input_text.strip():
            raise ValueError("input_text must be a non-empty string")
        now = utc_now()
        turn = TurnRecord(str(uuid4()), task_id, input_text, status, now, now)
        with self._session_factory.begin() as session:
            session.add(
                TurnModel(
                    turn_id=turn.turn_id,
                    task_id=turn.task_id,
                    input_text=turn.input_text,
                    status=turn.status,
                    created_at=to_text(turn.created_at),
                    updated_at=to_text(turn.updated_at),
                )
            )
        return turn

    def get(self, turn_id: str) -> TurnRecord:
        """Return a turn by identifier."""

        with self._session_factory() as session:
            row = session.get(TurnModel, turn_id)
        if row is None:
            raise KeyError(turn_id)
        return self._turn_from_model(row)

    def list_by_task(self, task_id: str) -> List[TurnRecord]:
        """List all turns for a task."""

        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(TurnModel)
                    .where(TurnModel.task_id == task_id)
                    .order_by(asc(TurnModel.created_at), asc(TurnModel.turn_id))
                )
                .scalars()
                .all()
            )
        return [self._turn_from_model(row) for row in rows]

    def update_status(self, turn_id: str, status: str) -> TurnRecord:
        """Update turn status."""

        self.get(turn_id)
        with self._session_factory.begin() as session:
            session.execute(
                update(TurnModel)
                .where(TurnModel.turn_id == turn_id)
                .values(status=status, updated_at=to_text(utc_now()))
            )
        return self.get(turn_id)

    def claim_pending(self, turn_id: str) -> bool:
        """Atomically advance a pending turn to running."""

        self.get(turn_id)
        now_text = to_text(utc_now())
        with self._session_factory.begin() as session:
            result = session.execute(
                update(TurnModel)
                .where(TurnModel.turn_id == turn_id, TurnModel.status == "pending")
                .values(status="running", updated_at=now_text)
            )
        return bool(result.rowcount)

    def get_first_for_task(self, task_id: str) -> TurnRecord:
        """Return the first turn associated with a task."""

        with self._session_factory() as session:
            row = session.execute(
                select(TurnModel)
                .where(TurnModel.task_id == task_id)
                .order_by(asc(TurnModel.created_at))
                .limit(1)
            ).scalar_one_or_none()
        if row is None:
            raise KeyError(task_id)
        return self._turn_from_model(row)

    def list_ids_by_task_ids(self, task_ids: list[str]) -> list[str]:
        """Return turn IDs for a set of task IDs."""

        from sqlalchemy import select

        if not task_ids:
            return []
        with self._session_factory() as session:
            return [
                row[0]
                for row in session.execute(
                    select(TurnModel.turn_id).where(TurnModel.task_id.in_(task_ids))
                ).all()
            ]

    def delete_by_ids(self, turn_ids: list[str]) -> None:
        """Delete turns by IDs."""

        from sqlalchemy import delete

        if not turn_ids:
            return
        with self._session_factory.begin() as session:
            session.execute(delete(TurnModel).where(TurnModel.turn_id.in_(turn_ids)))

    def _turn_from_model(self, row: TurnModel) -> TurnRecord:
        """Convert a turn ORM model to a domain record."""
        return TurnRecord(
            row.turn_id,
            row.task_id,
            row.input_text,
            row.status,
            from_text(row.created_at),
            from_text(row.updated_at),
        )
