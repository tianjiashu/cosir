"""任务运行切片 SQLAlchemy model。"""

from sqlalchemy import ForeignKey, Text, text
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
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    last_message_preview: Mapped[str] = mapped_column(Text, nullable=False)
    latest_turn_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


