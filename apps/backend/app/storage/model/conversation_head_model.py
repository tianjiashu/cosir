"""会话聚合版本头 SQLAlchemy 模型。"""

from sqlalchemy import ForeignKey, Index, Integer, text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class ConversationHeadModel(StorageBase):
    """``conversation_heads`` 表模型。

    每个 task 只有一个版本头。``revision`` 是该 task 会话事实的单调版本，
    ``message_sequence`` 用于分配消息在 task 内的稳定显示顺序。两者均由
    ``ConversationMutationWriter`` 在同一个 SQLite 写事务中更新。
    """

    __tablename__ = "conversation_heads"
    __table_args__ = (Index("uq_conversation_heads_task_id", "task_id", unique=True),)

    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=False)
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    message_sequence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
