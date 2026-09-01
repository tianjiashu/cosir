"""会话事实变更索引 SQLAlchemy 模型。"""

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class ConversationChangeModel(StorageBase):
    """``conversation_changes`` 表模型。

    该表只记录已经提交的事实变更索引，不复制消息内容、模型 token 或工具输出。
    ``(task_id, revision)`` 是数据库保证的 task 级唯一变更序列。
    """

    __tablename__ = "conversation_changes"
    __table_args__ = (
        Index("uq_conversation_changes_task_revision", "task_id", "revision", unique=True),
        Index("idx_conversation_changes_task_revision", "task_id", "revision"),
    )

    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    change_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    turn_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("turns.id"), nullable=True)
