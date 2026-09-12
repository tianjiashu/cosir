"""Conversation Task Context 持久化模型。"""

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class ConversationTaskContextModel(StorageBase):
    """``conversation_task_contexts`` 表：任务级上下文单条消息的持久化事实。

    按 (task, run) 一行存储一条 ``BaseMessage`` 的 JSON 序列化结果及其 Transport
    metadata，配合 ``sequence`` 维持跨 run 的全局插入顺序，``include_in_context``
    标记该消息是否纳入上下文视图。

    该表是上下文消息的**唯一持久化真相**，与
    :class:`app.models.conversation_task_context.ConversationTaskContextRecord` 一一对应；
    所有行↔对象映射必须经 record 的 ``_from_model`` / ``_to_model``。

    职责边界：
    - 负责：单条上下文消息的落盘（task_id、所属 run_id、消息 JSON、纳入标记、排序）。
    - 不负责：消息内容语义校验、上下文压缩策略（由上下文管理器负责）。
    """

    __tablename__ = "conversation_task_contexts"
    __table_args__ = (
        UniqueConstraint("task_id", "sequence", name="uq_task_context_task_seq"),
        UniqueConstraint(
            "task_id",
            "run_id",
            "tool_call_id",
            name="uq_task_context_task_run_tool_call",
        ),
        Index("idx_task_context_task_run", "task_id", "run_id"),
    )

    task_id: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("conversation_runs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    tool_call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_json: Mapped[str] = mapped_column(Text, nullable=False)
    transport_metadata_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    include_in_context: Mapped[bool] = mapped_column(default=True, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
