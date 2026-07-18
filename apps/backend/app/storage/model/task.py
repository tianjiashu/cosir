"""浠诲姟杩愯鍒囩墖 SQLAlchemy model銆?"""

from sqlalchemy import ForeignKey, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class WorkspaceModel(StorageBase):
    """`workspaces` 琛ㄦā鍨嬨€?"""

    __tablename__ = "workspaces"

    workspace_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class TaskModel(StorageBase):
    """`tasks` 琛ㄦā鍨嬨€?"""

    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, ForeignKey("workspaces.workspace_id"), nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'developer'"))
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    last_message_preview: Mapped[str] = mapped_column(Text, nullable=False)
    latest_turn_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class TurnModel(StorageBase):
    """`turns` 琛ㄦā鍨嬨€?"""

    __tablename__ = "turns"

    turn_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, ForeignKey("tasks.task_id"), nullable=False)
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class StepModel(StorageBase):
    """`steps` 琛ㄦā鍨嬨€?"""

    __tablename__ = "steps"

    step_id: Mapped[str] = mapped_column(Text, primary_key=True)
    turn_id: Mapped[str] = mapped_column(Text, ForeignKey("turns.turn_id"), nullable=False)
    step_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    input_summary: Mapped[str] = mapped_column(Text, nullable=False)
    output_summary: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class EventModel(StorageBase):
    """`events` 琛ㄦā鍨嬨€?"""

    __tablename__ = "events"

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, ForeignKey("tasks.task_id"), nullable=False)
    turn_id: Mapped[str | None] = mapped_column(Text, ForeignKey("turns.turn_id"))
    sequence: Mapped[int] = mapped_column(nullable=False)
    message_id: Mapped[str | None] = mapped_column(Text)
    tool_call_id: Mapped[str | None] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
