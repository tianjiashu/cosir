"""SQLite CRUD for durable run state."""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence
from uuid import uuid4

from sqlalchemy import asc, delete, select, update

from app.core.runs.records import RunRecord
from app.storage.database import create_session_factory
from app.storage.model.durable import DurableRunModel
from app.storage.schema import initialize_app_schema

_LOGGER = logging.getLogger("coding_agent.backend")


class DurableRunStore:
    """Read and write durable run state."""

    def __init__(self, database_path: Path) -> None:
        """Initialize the durable run store.

        Parameters:
            database_path: SQLite database path.

        Returns:
            None.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If schema initialization fails.

        Side effects:
            Initializes the application SQLite schema.
        """

        self._engine = initialize_app_schema(database_path)
        self._session_factory = create_session_factory(self._engine)

    def close(self) -> None:
        """Dispose the SQLite engine held by this store.

        Parameters:
            None.

        Returns:
            None.

        Raises:
            None.

        Side effects:
            Closes pooled SQLite connections.
        """

        self._engine.dispose()

    def create_for_turn(
        self,
        task_id: str,
        turn_id: str,
        status: str,
        thread_id: Optional[str] = None,
    ) -> RunRecord:
        """Create a run for a turn, or return the existing run.

        Parameters:
            task_id: Owning task identifier.
            turn_id: Owning turn identifier.
            status: Initial run status.
            thread_id: Optional stable runtime thread identifier.

        Returns:
            Created or existing run record.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If persistence fails.

        Side effects:
            May insert one row into ``durable_runs``.
        """

        existing = self.get_by_turn(turn_id)
        if existing is not None:
            return existing
        now = _utc_now()
        run = RunRecord(
            str(uuid4()),
            task_id,
            turn_id,
            thread_id or str(uuid4()),
            status,
            None,
            None,
            None,
            None,
            now,
            now,
        )
        with self._session_factory.begin() as session:
            session.add(_run_model(run))
        return run

    def get(self, run_id: str) -> RunRecord:
        """Return a run by id.

        Parameters:
            run_id: Run identifier.

        Returns:
            Matching run record.

        Raises:
            KeyError: If the run does not exist.

        Side effects:
            Opens a SQLite session.
        """

        with self._session_factory() as session:
            row = session.get(DurableRunModel, run_id)
        if row is None:
            raise KeyError(run_id)
        return _run_from_model(row)

    def get_by_turn(self, turn_id: str) -> Optional[RunRecord]:
        """Return the run for a turn when one exists.

        Parameters:
            turn_id: Turn identifier.

        Returns:
            Matching run record or None.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If querying fails.

        Side effects:
            Opens a SQLite session.
        """

        with self._session_factory() as session:
            row = session.execute(
                select(DurableRunModel).where(DurableRunModel.turn_id == turn_id)
            ).scalar_one_or_none()
        return _run_from_model(row) if row is not None else None

    def list_by_task(self, task_id: str) -> List[RunRecord]:
        """Return all runs for a task.

        Parameters:
            task_id: Task identifier.

        Returns:
            Runs ordered by creation time and run id.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If querying fails.

        Side effects:
            Opens a SQLite session.
        """

        with self._session_factory() as session:
            rows = session.execute(
                select(DurableRunModel)
                .where(DurableRunModel.task_id == task_id)
                .order_by(asc(DurableRunModel.created_at), asc(DurableRunModel.run_id))
            ).scalars().all()
        return [_run_from_model(row) for row in rows]

    def delete_by_task_ids(self, task_ids: Sequence[str]) -> list[str]:
        """Delete durable runs for a set of tasks.

        Parameters:
            task_ids: Task identifiers to delete.

        Returns:
            Deleted run identifiers.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If deletion fails.

        Side effects:
            Deletes matching rows from ``durable_runs``.
        """

        if not task_ids:
            return []
        run_ids: list[str] = []
        try:
            with self._session_factory.begin() as session:
                run_ids = [
                    row[0]
                    for row in session.execute(
                        select(DurableRunModel.run_id).where(DurableRunModel.task_id.in_(tuple(task_ids)))
                    ).all()
                ]
                if run_ids:
                    session.execute(delete(DurableRunModel).where(DurableRunModel.run_id.in_(run_ids)))
        except Exception:
            _LOGGER.exception(
                "durable_runs_delete_failed",
                extra={
                    "msg": "durable runs delete failed",
                    "data": {"task_count": len(task_ids), "operation": "delete_by_task_ids"},
                },
            )
            raise
        _LOGGER.info(
            "durable_runs_deleted",
            extra={
                "msg": "durable runs deleted",
                "data": {"task_count": len(task_ids), "run_count": len(run_ids)},
            },
        )
        return run_ids

    def mark_status(
        self,
        run_id: str,
        status: str,
        wait_reason: Optional[str] = None,
        active_step_id: Optional[str] = None,
        active_wait_id: Optional[str] = None,
        interruption_reason: Optional[str] = None,
    ) -> RunRecord:
        """Update run status fields.

        Parameters:
            run_id: Run identifier.
            status: New run status.
            wait_reason: Optional wait reason.
            active_step_id: Optional active step identifier.
            active_wait_id: Optional active wait identifier.
            interruption_reason: Optional interruption reason.

        Returns:
            Updated run record.

        Raises:
            KeyError: If the run does not exist.

        Side effects:
            Updates one ``durable_runs`` row.
        """

        with self._session_factory.begin() as session:
            result = session.execute(
                update(DurableRunModel)
                .where(DurableRunModel.run_id == run_id)
                .values(
                    status=status,
                    wait_reason=wait_reason,
                    active_step_id=active_step_id,
                    active_wait_id=active_wait_id,
                    interruption_reason=interruption_reason,
                    updated_at=_to_text(_utc_now()),
                )
            )
        if result.rowcount != 1:
            raise KeyError(run_id)
        return self.get(run_id)


def _utc_now() -> datetime:
    """Return the current UTC time."""

    return datetime.now(timezone.utc)


def _to_text(value: datetime) -> str:
    """Serialize a datetime to ISO-8601 text."""

    return value.isoformat()


def _from_text(value: str) -> datetime:
    """Parse ISO-8601 datetime text."""

    return datetime.fromisoformat(value)


def _run_model(run: RunRecord) -> DurableRunModel:
    """Convert a run record to a SQLAlchemy model."""

    return DurableRunModel(
        run_id=run.run_id,
        task_id=run.task_id,
        turn_id=run.turn_id,
        thread_id=run.thread_id,
        status=run.status,
        wait_reason=run.wait_reason,
        active_step_id=run.active_step_id,
        active_wait_id=run.active_wait_id,
        interruption_reason=run.interruption_reason,
        created_at=_to_text(run.created_at),
        updated_at=_to_text(run.updated_at),
    )


def _run_from_model(row: DurableRunModel) -> RunRecord:
    """Convert a SQLAlchemy model to a run record."""

    return RunRecord(
        row.run_id,
        row.task_id,
        row.turn_id,
        row.thread_id,
        row.status,
        row.wait_reason,
        row.active_step_id,
        row.active_wait_id,
        row.interruption_reason,
        _from_text(row.created_at),
        _from_text(row.updated_at),
    )
