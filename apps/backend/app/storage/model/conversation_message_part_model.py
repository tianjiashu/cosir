"""会话消息 part 事实 SQLAlchemy 模型。"""

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class ConversationMessagePartModel(StorageBase):
    """``conversation_message_parts`` 表模型。

    ``part_type`` 是可扩展的中性类型（如 ``text``、``reasoning``、``tool-call``）；
    文本内容存于 ``text``，其余结构化字段存于 ``data_json``。模型不依赖
    assistant-ui 类型。
    """

    __tablename__ = "conversation_message_parts"
    __table_args__ = (
        Index(
            "uq_conversation_message_parts_message_sequence",
            "message_id",
            "sequence",
            unique=True,
        ),
        Index("idx_conversation_message_parts_message", "message_id", "sequence"),
    )

    message_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversation_messages.id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    part_type: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="complete")
