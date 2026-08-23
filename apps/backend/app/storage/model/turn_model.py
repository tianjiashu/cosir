"""Turn（任务轮次）SQLAlchemy ORM 模型。

本模块只定义 ``turns`` 单表的列结构与 StorageBase 继承关系，不含查询逻辑。
"""

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.enums.turn_status import TurnStatus
from app.storage.model.base import StorageBase

_ALLOWED_TURN_STATUSES = tuple(status.value for status in TurnStatus)


class TurnModel(StorageBase):
    """``turns`` 表 ORM 模型，承载单次任务轮次的持久化字段。

    字段语义：
    - ``turn_id``：轮次主键（UUID 文本）。
    - ``task_id``：归属任务外键（``tasks.task_id``），不可空。
    - ``input_text``：本轮用户输入文本，不可空。
    - ``status``：轮次状态机（运行期枚举，由 TurnStatus 约束）。
    - ``end_reason``：终态原因（客户端断开 / 失败 / 正常完成等），可选。
    - ``response_text``：模型最终回复文本，可选。
    - ``created_at`` / ``updated_at``：时间戳文本（项目约定以文本存储）。
    - ``agent_id``：本轮使用的 Agent 条目标识，可选。
    - ``product_name``：本轮使用模型的厂商，可选。
    - ``model_name``：本轮使用的模型条目标识，可选。
    - ``paths``：本轮涉及的文件路径集合（JSON 文本存储），可选。
    - ``thinking``：是否开启推理模式，可选。
    - ``reasoning_effort``：推理强度（``low`` / ``high`` / ``max``），可选。
    """

    __tablename__ = "turns"

    # 数据库层约束：``reasoning_effort`` 为可选字段，非 NULL 时只允许
    # ``low`` / ``high`` / ``max`` 三值（与 ModelSettings.reasoning_effort 取值一致）。
    # 放在 DDL 层而非仅 Python 校验，确保任意写入路径（ORM / 原生 SQL / 批量插入）
    # 都无法写入非法值。
    __table_args__ = (
        CheckConstraint(
            "reasoning_effort IS NULL OR reasoning_effort IN ('low', 'high', 'max')",
            name="ck_turns_reasoning_effort",
        ),
        # 数据库层约束：``status`` 只允许 TurnStatus 枚举声明的取值，
        # 置于 DDL 层确保任意写入路径（ORM / 原生 SQL / 批量插入）都无法写入非法状态。
        # 取值集合由 TurnStatus 枚举单一事实来源驱动，避免与枚举漂移。
        CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in _ALLOWED_TURN_STATUSES)})",
            name="ck_turns_status",
        ),
    )

    turn_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, ForeignKey("tasks.task_id"), nullable=False)
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    end_reason: Mapped[str | None] = mapped_column(Text)
    response_text: Mapped[str | None] = mapped_column(Text)
    agent_id: Mapped[str | None] = mapped_column(Text)
    product_name: Mapped[str | None] = mapped_column(Text)
    model_name: Mapped[str | None] = mapped_column(Text)
    paths: Mapped[list[str] | None] = mapped_column(Text)
    thinking: Mapped[bool | None] = mapped_column(Boolean)
    reasoning_effort: Mapped[str | None] = mapped_column(Text)  # low/high/max
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
