"""SQLAlchemy models for durable run state."""

from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class DurableRunModel(StorageBase):
    """SQLAlchemy model for the ``durable_runs`` table."""

    __tablename__ = "durable_runs"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    turn_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    wait_reason: Mapped[str | None] = mapped_column(Text)
    active_step_id: Mapped[str | None] = mapped_column(Text)
    active_wait_id: Mapped[str | None] = mapped_column(Text)
    interruption_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
