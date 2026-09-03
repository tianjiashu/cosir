"""Task 一对一 ConversationState 快照 SQLAlchemy model。"""

from sqlalchemy import ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class ConversationTaskSnapshotModel(StorageBase):
    """``conversation_task_snapshots`` 表模型。

    ``task_id`` 唯一约束保证一个 Task 只有一份持久化快照；``id`` 仍由项目统一的
    ``StorageBase`` 提供，避免为单表模型引入第二套 ORM 基类约定。
    """

    __tablename__ = "conversation_task_snapshots"
    __table_args__ = (  # type: ignore[assignment]
        UniqueConstraint("task_id", name="uq_conversation_task_snapshots_task"),
    )

    task_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("tasks.id"),
        nullable=False,
    )
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
