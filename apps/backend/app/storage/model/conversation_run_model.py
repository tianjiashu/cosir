"""Conversation Run 持久化模型。"""

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.enums.conversation_run_status import ConversationRunStatus
from app.storage.model.base import StorageBase


class ConversationRunModel(StorageBase):
    """``conversation_runs`` 表模型：一次 Agent 运行的持久化事实。

    Run 是「一次 Agent 执行」的独立实体：承载输入文本、模型路由（provider/model）、
    推理深度、终态原因、回复正文与工作流版本。运行标识
    即本行 ``id``，由 ``StorageBase`` 提供自增主键。

    职责边界：
    - 负责：单次运行的输入、模型路由、状态机与终态结果。
    - 不负责：Transport 命令幂等占用（由 ``conversation_commands`` 表持有指向本表的
      ``run_id`` 外键）、消息/工具调用等 canonical 会话事实（各自独立表）。
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
    input_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    agent_id: Mapped[str | None] = mapped_column(Text)
    provider_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("providers.id"))
    model_name: Mapped[str | None] = mapped_column(String)
    image_paths: Mapped[list[str] | None] = mapped_column(JSON)
    reasoning_effort: Mapped[str | None] = mapped_column(Text)
    end_reason: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ConversationRunStatus.PENDING.value
    )
