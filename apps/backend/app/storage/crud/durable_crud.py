"""SQLite CRUD for durable run state."""

import logging
from collections.abc import Sequence
from uuid import uuid4

from sqlalchemy import asc, delete, select, update
from sqlalchemy.orm import sessionmaker

from app.models import RunRecord
from app.storage.model.durable_model import DurableRunModel
from app.utils.datetime_utils import utc_now, to_text, from_text

_LOGGER = logging.getLogger("coding_agent.backend")


class DurableRunStore:
    """Read and write durable run state."""

    def __init__(self, session_factory: sessionmaker) -> None:
        """Initialize the durable run store.

        Parameters:
            session_factory: SQLAlchemy session factory (from ``StorageContext``).
        """

        self._session_factory = session_factory

    def create_for_turn(
            self,
            task_id: str,
            turn_id: str,
            status: str,
            thread_id: str | None = None,
    ) -> RunRecord:
        """Create a run for a turn, or return the existing run.

        Parameters:
            task_id: Owning task identifier.
            turn_id: Owning turn identifier.
            status: Initial run status.
            thread_id: Optional stable tool_execute thread identifier.

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
        now = utc_now()
        run = RunRecord(
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
            session.add(self._run_model(run))
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
        return self._run_from_model(row)

    def get_by_turn(self, turn_id: str) -> RunRecord | None:
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
        return self._run_from_model(row) if row is not None else None

    def list_by_task(self, task_id: str) -> list[RunRecord]:
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
            rows = (
                session.execute(
                    select(DurableRunModel)
                    .where(DurableRunModel.task_id == task_id)
                    .order_by(asc(DurableRunModel.created_at), asc(DurableRunModel.run_id))
                )
                .scalars()
                .all()
            )
        return [self._run_from_model(row) for row in rows]

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
                        select(DurableRunModel.run_id).where(
                            DurableRunModel.task_id.in_(tuple(task_ids))
                        )
                    ).all()
                ]
                if run_ids:
                    session.execute(
                        delete(DurableRunModel).where(DurableRunModel.run_id.in_(run_ids))
                    )
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
            wait_reason: str | None = None,
            active_step_id: str | None = None,
            active_wait_id: str | None = None,
            interruption_reason: str | None = None,
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
                    updated_at=to_text(utc_now()),
                )
            )
        if result.rowcount != 1:
            raise KeyError(run_id)
        return self.get(run_id)

    def _run_from_model(self, row: DurableRunModel) -> RunRecord:
        """Convert a SQLAlchemy model to a run record."""

        return RunRecord(
            row.task_id,
            row.turn_id,
            row.thread_id,
            row.status,
            row.wait_reason,
            row.active_step_id,
            row.active_wait_id,
            row.interruption_reason,
            from_text(row.created_at),
            from_text(row.updated_at),
        )

    def _run_model(self, run: RunRecord) -> DurableRunModel:
        """Convert a run record to a SQLAlchemy model."""

        return DurableRunModel(
            task_id=run.task_id,
            turn_id=run.turn_id,
            thread_id=run.thread_id,
            status=run.status,
            wait_reason=run.wait_reason,
            active_step_id=run.active_step_id,
            active_wait_id=run.active_wait_id,
            interruption_reason=run.interruption_reason,
            created_at=to_text(run.created_at),
            updated_at=to_text(run.updated_at),
        )
