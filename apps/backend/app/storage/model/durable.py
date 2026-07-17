"""Durable run SQLAlchemy model。"""

from sqlalchemy import Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class DurableRunModel(StorageBase):
    """`durable_runs` 表模型。"""

    __tablename__ = "durable_runs"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    turn_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    wait_reason: Mapped[str | None] = mapped_column(Text)
    active_step_id: Mapped[str | None] = mapped_column(Text)
    active_wait_id: Mapped[str | None] = mapped_column(Text)
    last_checkpoint_id: Mapped[str | None] = mapped_column(Text)
    interruption_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class ResumeCommandModel(StorageBase):
    """`resume_commands` 表模型。"""

    __tablename__ = "resume_commands"
    __table_args__ = (UniqueConstraint("idempotency_key"),)

    command_id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    applied_at: Mapped[str | None] = mapped_column(Text)
