"""任务运行切片 SQLAlchemy model。"""

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class TaskModel(StorageBase):
    """`tasks` 表模型。"""

    __tablename__ = "tasks"

    workspace_id: Mapped[int] = mapped_column(Integer, ForeignKey("workspaces.id"), nullable=False)
    creation_command_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    task_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'user'"), default="user"
    )
    parent_task_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tasks.id"), nullable=True
    )
    parent_run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("conversation_runs.id"), nullable=True
    )
    delegation_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("delegations.id"), nullable=True
    )
    context_usage_used: Mapped[int | None] = mapped_column(
        Integer,
        default=0,
        comment="最近一次上下文窗口已用 token（total 由 resolve_context_window 动态计算，不落库）",
    )

    __table_args__ = (
        Index("idx_tasks_parent_task_id", "parent_task_id"),
        Index("uq_tasks_delegation_id", "delegation_id", unique=True),
        Index(
            "uq_tasks_workspace_creation_command",
            "workspace_id",
            "creation_command_id",
            unique=True,
        ),
        {"sqlite_autoincrement": True},
    )
