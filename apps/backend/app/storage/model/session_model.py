"""会话运行切片 SQLAlchemy model。"""

from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.session.session_kind import SessionType
from app.storage.model.base import StorageBase


class SessionModel(StorageBase):
    """`sessions` 表模型，持有当前 Run 引用与会话容器事实。"""

    __tablename__ = "sessions"

    workspace_id: Mapped[int] = mapped_column(Integer, ForeignKey("workspaces.id"), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Session 创建时固化的工具面（``SessionToolSurface``）；不包含 handler、权限运行态或版本号。
    tool_surface: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    session_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'user'"), default=SessionType.USER
    )
    parent_session_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("sessions.id"), nullable=True
    )
    # 派生深度：用户新建与用户 fork 为 0，派生方按 ``core.tool.multi_agent.derivation.child_depth`` 写入；
    # 创建后不变。
    derivation_depth: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    # 会话的终端权限（会话树的安全策略，不是每条消息的发起配置）：Agent 每条命令启动时按本会话与所有权
    # 祖先中最严的那个执行（``SessionService.effective_terminal_mode``）。
    terminal_permission_mode: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'request_approval'"), default="request_approval"
    )
    current_run_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    __table_args__ = (
        Index("idx_sessions_parent_session_id", "parent_session_id"),
        CheckConstraint(
            "terminal_permission_mode IN ('request_approval', 'full_access')",
            name="ck_sessions_terminal_permission_mode",
        ),
    )
