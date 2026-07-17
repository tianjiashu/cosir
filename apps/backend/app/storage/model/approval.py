"""审批 SQLAlchemy model。"""

from sqlalchemy import Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class ApprovalRequestModel(StorageBase):
    """`approval_requests` 表模型。"""

    __tablename__ = "approval_requests"

    approval_id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    step_id: Mapped[str | None] = mapped_column(Text)
    tool_call_id: Mapped[str | None] = mapped_column(Text)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    permission: Mapped[str] = mapped_column(Text, nullable=False)
    risk_level: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[str | None] = mapped_column(Text)


class ApprovalDecisionModel(StorageBase):
    """`approval_decisions` 表模型。"""

    __tablename__ = "approval_decisions"
    __table_args__ = (
        UniqueConstraint("approval_id"),
        UniqueConstraint("idempotency_key"),
    )

    decision_id: Mapped[str] = mapped_column(Text, primary_key=True)
    approval_id: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
