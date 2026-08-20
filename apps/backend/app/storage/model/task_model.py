"""任务运行切片 SQLAlchemy model。"""

from sqlalchemy import ForeignKey, Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class TaskModel(StorageBase):
    """`tasks` 表模型。"""

    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.workspace_id"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'developer'"))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)

    task_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'user'"), default="user"
    )
    parent_task_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("tasks.task_id"), nullable=True
    )
    parent_turn_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("turns.turn_id"), nullable=True
    )
    delegation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_usage_used: Mapped[int | None] = mapped_column(
        Integer,
        default=0,
        comment="最近一次上下文窗口已用 token（total 由 resolve_context_window 动态计算，不落库）",
    )

    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        Index("idx_tasks_parent_task_id", "parent_task_id"),
        Index("uq_tasks_delegation_id", "delegation_id", unique=True),
    )
