"""Conversation Run 持久化模型。"""

from typing import get_args

from pydantic import JsonValue
from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.enums.reasoning_effort import ReasoningEffort
from app.models.enums.run_status import RunStatus
from app.models.run_extra import RunExtra
from app.storage.model.base import StorageBase


class RunModel(StorageBase):
    """Run 生命周期、执行配置与恢复进度的唯一持久化记录。

    用户正文和附件归 message_queue/context；Run 只保留模型路由、冻结选项和终态结果。
    context_window_total 与本 Run 的 usage 配对，不随后续配置变化。
    """

    __tablename__ = "runs"
    __table_args__ = (
        Index("idx_runs_session_created", "session_id", "created_at"),
        Index("idx_runs_status", "status"),
        CheckConstraint(
            "status IN ({})".format(
                ", ".join(repr(status.value) for status in RunStatus)
            ),
            name="ck_runs_status",
        ),
        CheckConstraint(
            "reasoning_effort IS NULL OR reasoning_effort IN ({})".format(
                ", ".join(repr(effort) for effort in get_args(ReasoningEffort))
            ),
            name="ck_runs_reasoning_effort",
        ),
    )

    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("sessions.id"), nullable=False)
    execution_data: Mapped[dict[str, JsonValue]] = mapped_column(JSON, nullable=False)
    agent_id: Mapped[str | None] = mapped_column(Text)
    # 模型 id（「连接 id/模型名」）；模型在配置文件里，没有外键，删除模型后这里保留原 id。
    model_config_id: Mapped[str | None] = mapped_column(Text)
    # 与本次 Run 的 usage 配对，避免 Session 后续切换模型后改变历史占用比例。
    context_window_total: Mapped[int | None] = mapped_column(Integer)
    # 本 Run 选定的推理强度；None 表示交给 Agent 偏好与模型默认值。
    reasoning_effort: Mapped[str | None] = mapped_column(String(8))
    end_reason: Mapped[str | None] = mapped_column(Text)
    final_output: Mapped[str | None] = mapped_column(Text)
    usage_json: Mapped[str | None] = mapped_column(Text)
    error_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # 严格结构见 ``RunExtra``；状态迁移历史由 ``RunCrud`` 用 SQLite JSON 函数原地追加。
    extra: Mapped[dict[str, JsonValue]] = mapped_column(
        JSON, nullable=False, default=lambda: RunExtra().model_dump(mode="json")
    )
