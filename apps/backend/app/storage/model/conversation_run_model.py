"""Conversation Run 持久化模型。"""

from pydantic import JsonValue
from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.enums.conversation_run_status import ConversationRunStatus
from app.storage.model.base import StorageBase


class ConversationRunModel(StorageBase):
    """Run 生命周期、执行配置与恢复进度的唯一持久化记录。

    用户正文和附件归 inbox/context；Run 只保留模型路由、冻结选项和终态结果。
    context_window_total 与本 Run 的 usage 配对，不随后续配置变化。
    """

    __tablename__ = "conversation_runs"
    __table_args__ = (
        Index("idx_conversation_runs_task_created", "task_id", "created_at"),
        Index("idx_conversation_runs_status", "status"),
        CheckConstraint(
            "status IN ({})".format(
                ", ".join(repr(status.value) for status in ConversationRunStatus)
            ),
            name="ck_conversation_runs_status",
        ),
    )

    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=False)
    execution_data: Mapped[dict[str, JsonValue]] = mapped_column(JSON, nullable=False)
    agent_id: Mapped[str | None] = mapped_column(Text)
    model_config_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("model_configs.id", ondelete="SET NULL")
    )
    # 与本次 Run 的 usage 配对，避免 Task 后续切换模型后改变历史占用比例。
    context_window_total: Mapped[int | None] = mapped_column(Integer)
    options: Mapped[dict[str, JsonValue]] = mapped_column(JSON, nullable=False)
    end_reason: Mapped[str | None] = mapped_column(Text)
    final_output: Mapped[str | None] = mapped_column(Text)
    usage_json: Mapped[str | None] = mapped_column(Text)
    error_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ConversationRunStatus.PENDING.value
    )
