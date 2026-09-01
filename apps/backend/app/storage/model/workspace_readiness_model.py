"""``workspace_readiness`` 表模型。

该表保存每个 workspace 最近一次准备流程的权威 snapshot。它不是事件表：每个
workspace 只有一行，``revision`` 在每次状态提交时递增，SSE 只负责通知状态可能已变化。
"""

from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class WorkspaceReadinessModel(StorageBase):
    """每个 workspace 一行的准备状态 snapshot。"""

    __tablename__ = "workspace_readiness"

    workspace_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_taken: Mapped[str] = mapped_column(Text, nullable=False)
    files_changed: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
