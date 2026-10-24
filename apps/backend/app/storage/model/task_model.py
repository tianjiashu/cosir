"""任务运行切片 SQLAlchemy model。"""

from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class TaskModel(StorageBase):
    """`tasks` 表模型，持有当前 Run 引用与任务容器事实。"""

    __tablename__ = "tasks"

    workspace_id: Mapped[int] = mapped_column(Integer, ForeignKey("workspaces.id"), nullable=False)
    creation_command_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Task 创建时冻结的模型可见工具 schema；本字段不包含 handler、权限运行态或版本号。
    tool_schemas: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )

    task_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'user'"), default="user"
    )
    parent_task_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tasks.id"), nullable=True
    )
    current_run_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("conversation_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    __table_args__ = (
        Index("idx_tasks_parent_task_id", "parent_task_id"),
        Index(
            "uq_tasks_workspace_creation_command",
            "workspace_id",
            "creation_command_id",
            unique=True,
        ),
    )
