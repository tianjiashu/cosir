"""会话工具调用事实 SQLAlchemy 模型。"""

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class ConversationToolCallModel(StorageBase):
    """``conversation_tool_calls`` 表模型。

    工具调用保存结构化参数、最终结果和生命周期状态；逐 token/逐 stdout 的临时增量
    不写入本表。``turn_id`` 同样是当前 turns → ConversationRun 迁移期的关联字段。
    """

    __tablename__ = "conversation_tool_calls"
    __table_args__ = (
        Index("uq_conversation_tool_calls_task_call", "task_id", "tool_call_id", unique=True),
        Index("idx_conversation_tool_calls_turn", "turn_id"),
    )

    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=False)
    turn_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("turns.id"), nullable=True)
    message_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("conversation_messages.id"), nullable=True
    )
    part_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("conversation_message_parts.id"), nullable=True
    )
    tool_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    args_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
