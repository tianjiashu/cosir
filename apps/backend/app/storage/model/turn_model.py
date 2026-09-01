"""Turn（任务轮次）SQLAlchemy ORM 模型。

本模块只定义 ``turns`` 单表的列结构与 StorageBase 继承关系，不含查询逻辑。
"""

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.enums.turn_status import TurnStatus
from app.storage.model.base import StorageBase

_ALLOWED_TURN_STATUSES = tuple(status.value for status in TurnStatus)


class TurnModel(StorageBase):
    """``turns`` 表 ORM 模型，承载单次任务轮次的持久化字段。

    字段语义：
    - ``id``：整数自增主键（由 StorageBase 提供），轮次唯一标识。
    - ``task_id``：归属任务外键（``tasks.id``），不可空。
    - ``input_text``：本轮用户输入文本，不可空。
    - ``status``：轮次状态机（运行期枚举，由 TurnStatus 约束）。
    - ``end_reason``：终态原因（客户端断开 / 失败 / 正常完成等），可选。
    - ``response_text``：模型最终回复文本，可选。
    - ``created_at`` / ``updated_at``：时间戳文本（项目约定以文本存储）。
    - ``agent_id``：本轮使用的 Agent 条目标识，可选。
    - ``provider_id``：本轮使用模型的厂商（``providers.id`` 外键），可选。
    - ``model_name``：本轮使用的模型 litellm 路由名（如 ``deepseek/deepseek-v4-flash``），
        可选；非外键，仅作运行期模型标识持久化。
    - ``image_paths``：本轮涉及的图片路径集合（仅图片，供多模态通道使用），
        文件/目录/链接已固化进 ``input_text``，可选。
    - ``reasoning_effort``：推理强度（``low`` / ``high`` / ``max``），可选。
    - ``fencing_version``：执行租约的 fencing 版本，每次成功认领 lease 时递增；
      过期 executor 携带旧版本写入事实时会被 ``ConversationMutationWriter`` 拒绝。
    - ``executor_lease_owner`` / ``executor_lease_expires_at``：当前有效执行者的租约
      持有者与过期时间；为空表示无人持有租约。
    - ``workflow_version``：本次运行所绑定工作流图的版本标识，用于判定暂停 checkpoint
      能否被当前图安全恢复。
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

    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=False)
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    end_reason: Mapped[str | None] = mapped_column(Text)
    response_text: Mapped[str | None] = mapped_column(Text)
    agent_id: Mapped[str | None] = mapped_column(Text)
    provider_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("providers.id"), nullable=True
    )
    model_name: Mapped[str | None] = mapped_column(String, nullable=True)
    image_paths: Mapped[list[str] | None] = mapped_column(Text)
    reasoning_effort: Mapped[str | None] = mapped_column(Text)  # low/high/max
    extra: Mapped[str | None] = mapped_column(JSON, nullable=False, doc="额外信息存储")
    # ConversationMutationWriter 的过渡运行身份校验。未来迁移为 ConversationRun 后，
    # 该字段应随运行 lease/fencing 事实迁移。
    fencing_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    executor_lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    executor_lease_expires_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    workflow_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="react_like_v1", server_default=text("'react_like_v1'")
    )
