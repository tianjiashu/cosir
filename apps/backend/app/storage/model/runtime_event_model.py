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

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class RuntimeEventModel(StorageBase):
    """``runtime_events`` 表模型：运行时事件的持久化存储。"""

    __tablename__ = "runtime_events"

    turn_id: Mapped[str] = mapped_column(Text, nullable=False, primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, primary_key=True)
    event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
