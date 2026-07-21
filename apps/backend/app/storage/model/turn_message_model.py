"""每轮消息轨迹 SQLAlchemy model（跨轮记忆 + 历史回放）。"""

from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class TurnMessageModel(StorageBase):
    """``turn_messages`` 表模型：承载某 turn 下的有序消息轨迹。

    每个 turn 的消息按 ``sequence`` 有序存储，模型无关（role + content_text +
    可选工具元数据 JSON）。用于跨轮记忆拼接与历史回放重建 transcript。
    """

    __tablename__ = "turn_messages"

    turn_id: Mapped[str] = mapped_column(
        Text, ForeignKey("turns.turn_id"), nullable=False, primary_key=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, primary_key=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text)
