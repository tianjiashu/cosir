"""``terminal_sessions`` table ORM model."""

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class TerminalSessionModel(StorageBase):
    """终端 session 的持久化元数据。

    PTY、worker 连接和输出缓存不写入此表；该表只记录 session 身份、归属、启动参数
    和最后已知生命周期状态。
    """

    __tablename__ = "terminal_sessions"
    __table_args__ = (
        Index("idx_terminal_sessions_task_status", "task_id", "status"),
        Index("idx_terminal_sessions_workspace_status", "workspace_id", "status"),
        CheckConstraint(
            "status IN ('starting', 'running', 'exited', 'interrupted', 'failed', 'closed')",
            name="ck_terminal_sessions_status",
        ),
        {"sqlite_autoincrement": True},
    )

    session_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=False)
    workspace_id: Mapped[int] = mapped_column(Integer, ForeignKey("workspaces.id"), nullable=False)
    created_by_run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("conversation_runs.id"), nullable=True
    )
    initial_cwd: Mapped[str] = mapped_column(Text, nullable=False)
    shell_kind: Mapped[str] = mapped_column(Text, nullable=False)
    shell_executable: Mapped[str] = mapped_column(Text, nullable=False)
    worker_instance_id: Mapped[str] = mapped_column(Text, nullable=False)
    worker_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="starting")
    end_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cols: Mapped[int] = mapped_column(Integer, nullable=False)
    rows: Mapped[int] = mapped_column(Integer, nullable=False)
    last_activity_at: Mapped[str] = mapped_column(Text, nullable=False)
    ended_at: Mapped[str | None] = mapped_column(Text, nullable=True)
