"""SQLAlchemy mapping for persisted delegations."""

from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class DelegationModel(StorageBase):
    """Map the ``delegations`` table."""

    __tablename__ = "delegations"

    delegation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    parent_turn_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    child_turn_id: Mapped[str] = mapped_column(Text, nullable=False)
    parent_agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    child_agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    delegation_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    requested_tools: Mapped[str] = mapped_column(Text, nullable=False)
    effective_tools: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
