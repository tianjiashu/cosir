"""``runtime_events`` 表模型：承载单次 turn 执行过程中的完整事件流。

每个 runtime event（如 ``model_thinking_delta``、``tool_call_requested``、
``model_output_delta`` 等）按 turn_id 聚合有序存储，用于：
- 历史回看时重建完整 timeline（思考过程 / 工具调用 / 状态变更）
- 重启后恢复前端渲染（不再仅依赖 ``TurnRecord.response_text``）

参数:
    无。

异常:
    无。

副作用:
    注册 ``runtime_events`` 表到统一 metadata。
"""

from sqlalchemy import Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class RuntimeEventModel(StorageBase):
    """``runtime_events`` 表模型：运行时事件的持久化存储。

    主键为单列 ``event_id``（全局唯一），``turn_id`` 为可空普通列——
    ``RuntimeEvent.turn_id: str | None`` 契约允许 None 事件（如全局/无 turn 归属
    的事件）正常落库，不再受 NOT NULL 复合主键约束。
    ``(turn_id, sequence)`` 唯一索引保留原复合主键的唯一性语义（同 turn 内
    sequence 不重复，并发分配冲突仍可触发重试）；SQLite 唯一索引对 NULL 列不做
    唯一性约束，因此多个 ``turn_id=None`` 事件可共存。
    """

    __tablename__ = "runtime_events"
    __table_args__ = (
        Index("uq_runtime_events_turn_sequence", "turn_id", "sequence", unique=True),
    )

    event_id: Mapped[str] = mapped_column(Text, nullable=False, primary_key=True)
    turn_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
