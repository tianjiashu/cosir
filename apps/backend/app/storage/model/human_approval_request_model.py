"""持久化的人工作业审批事实。"""

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class HumanApprovalRequestModel(StorageBase):
    """记录一次工具/运行审批及其最终决策。"""

    __tablename__ = "human_approval_requests"

    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=False)
    run_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("conversation_runs.id"))
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    decision: Mapped[str | None] = mapped_column(String(32))
    decision_reason: Mapped[str | None] = mapped_column(Text)
