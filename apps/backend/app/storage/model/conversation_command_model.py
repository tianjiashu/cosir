"""Assistant Transport 命令持久化模型。"""

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class ConversationCommandModel(StorageBase):
    """``conversation_commands`` 表模型：一次 Transport 命令的幂等占用事实。

    命令行只负责幂等键占用与「命令 → Run」的关联：``(task_id, command_id)`` 唯一索引是
    并发裁判，``run_id`` 外键指向 ``conversation_runs.id`` 表达本次命令实际驱动的运行。

    职责边界：
    - 负责：命令幂等占用、载荷指纹、失败错误码、关联的 Run 标识。
    - 不负责：运行态与终态（运行状态、模型路由、执行租约、终态结果均在
      ``conversation_runs`` 表）。
    """

    __tablename__ = "conversation_commands"
    __table_args__ = (
        Index("uq_conversation_commands_task_command", "task_id", "command_id", unique=True),
        Index("idx_conversation_commands_run", "run_id"),
    )

    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=False)
    command_id: Mapped[str] = mapped_column(String(128), nullable=False)
    command_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("conversation_runs.id"), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(128))
