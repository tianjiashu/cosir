"""会话运行切片 SQLAlchemy model。"""

from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.session_kind import SessionType
from app.storage.model.base import StorageBase


class SessionModel(StorageBase):
    """`sessions` 表模型，持有当前 Run 引用与会话容器事实。"""

    __tablename__ = "sessions"

    workspace_id: Mapped[int] = mapped_column(Integer, ForeignKey("workspaces.id"), nullable=False)
    creation_command_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Session 创建时冻结的模型可见工具 schema；本字段不包含 handler、权限运行态或版本号。
    tool_schemas: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )

    session_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'user'"), default=SessionType.USER
    )
    parent_session_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("sessions.id"), nullable=True
    )
    # 派生深度：用户新建与用户 fork 为 0，派生方按 ``core.tool.derivation.child_depth`` 写入；
    # 创建后不变。
    derivation_depth: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    current_run_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    __table_args__ = (
        Index("idx_sessions_parent_session_id", "parent_session_id"),
        Index(
            "uq_sessions_workspace_creation_command",
            "workspace_id",
            "creation_command_id",
            unique=True,
        ),
    )
