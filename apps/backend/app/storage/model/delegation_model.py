"""SQLAlchemy mapping for persisted delegations."""

from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class DelegationModel(StorageBase):
    """Map the ``delegations`` table."""

    __tablename__ = "delegations"

    task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tasks.id"), nullable=False, index=True
    )
    parent_run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversation_runs.id"), nullable=False, index=True
    )
    child_run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversation_runs.id"), nullable=True
    )
    child_task_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tasks.id"), nullable=True
    )
    parent_agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    child_agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    effective_tools: Mapped[str] = mapped_column(Text, nullable=False)
