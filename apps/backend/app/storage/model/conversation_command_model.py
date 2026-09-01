"""Assistant Transport 命令持久化模型。"""

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class ConversationCommandModel(StorageBase):
    """记录一次 Transport 命令及其关联的 Agent turn。"""

    __tablename__ = "conversation_commands"
    __table_args__ = (
        Index("uq_conversation_commands_task_command", "task_id", "command_id", unique=True),
        CheckConstraint(
            "status IN ('received', 'processing', 'completed', 'failed', 'cancelled')",
            name="ck_conversation_commands_status",
        ),
    )

    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=False)
    command_id: Mapped[str] = mapped_column(String(128), nullable=False)
    command_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    turn_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("turns.id"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="received")
    error_code: Mapped[str | None] = mapped_column(Text)
