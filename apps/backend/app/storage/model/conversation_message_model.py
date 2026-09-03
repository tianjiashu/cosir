"""会话消息事实 SQLAlchemy 模型。"""

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class ConversationMessageModel(StorageBase):
    """``conversation_messages`` 表模型。

    消息是可重建 Chat UI 的 canonical fact。当前 ``run_id`` 关联既有 turns 表，
    作为 ConversationRun 完成迁移前的运行身份过渡字段。
    """

    __tablename__ = "conversation_messages"
    __table_args__ = (
        Index("uq_conversation_messages_task_sequence", "task_id", "sequence", unique=True),
        Index("idx_conversation_messages_task_created", "task_id", "created_at"),
        CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool')",
            name="ck_conversation_messages_role",
        ),
    )

    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=False)
    run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("conversation_runs.id"), nullable=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="complete")
    end_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
