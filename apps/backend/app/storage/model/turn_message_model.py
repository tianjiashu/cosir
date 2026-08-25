"""每轮消息轨迹 SQLAlchemy model（跨轮记忆 + 历史回放）。"""

from sqlalchemy import Boolean, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class TurnMessageModel(StorageBase):
    """``turn_messages`` 表模型：承载某 turn 下的有序消息轨迹。

    每个 turn 的消息按 ``sequence`` 有序存储，模型无关（role + content_text +
    可选工具元数据 JSON）。用于跨轮记忆拼接与历史回放重建 transcript。
    """

    __tablename__ = "turn_messages"
    __table_args__ = (
        UniqueConstraint("turn_id", "sequence", name="uq_turn_messages_turn_sequence"),
    )

    turn_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("turns.id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text)

    # 是否纳入Agent上下文
    in_context: Mapped[bool] = mapped_column(Boolean, default=True)
