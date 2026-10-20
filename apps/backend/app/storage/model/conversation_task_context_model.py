"""Conversation Task Context 持久化模型。"""

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class ConversationTaskContextModel(StorageBase):
    """``conversation_task_contexts`` 表：任务级上下文单条消息的持久化事实。

    一行存储一条 SDK 请求消息 JSON 正文及按角色约束的 metadata，
    配合 ``sequence`` 维持跨 run 的全局插入顺序，``include_in_context``
    标记该消息是否纳入上下文视图；``project_to_transport`` 独立标记是否向前端投影。

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
        Index("idx_task_context_task_run", "task_id", "run_id"),
    )

    task_id: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("conversation_runs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    message_id: Mapped[str] = mapped_column(String(36), nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    message_json: Mapped[str] = mapped_column(Text, nullable=False)
    include_in_context: Mapped[bool] = mapped_column(default=True, nullable=False)
    # 模型上下文成员身份与前端可见性是独立事实。
    project_to_transport: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
