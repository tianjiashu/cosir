"""Task 待消费用户输入；消费后正文转入 context 并删除本行。"""

from typing import Any, get_args

from sqlalchemy import JSON, Enum, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.models.conversation_input import InputDelivery
from app.storage.model.base import StorageBase


class TaskInputModel(StorageBase):
    """``task_inputs`` 表：尚未被模型消费的用户输入。

    行身份（``StorageBase.id``）就是这条用户消息的稳定身份：队列投影、编辑定位与消费进
    context 后的消息 id 都用它，不再另存一列 UUID。库内为整数主键，跨进程边界的字符串形式
    由 service 层统一渲染。

    ``payload`` 只放写入后不再变化的输入内容与发起配置；``delivery`` 是这条输入的调度状态
    （新 Run 开始时会被统一转为 ``immediate``），所以单独成列，按 SQL 过滤与批量更新，不改写
    payload。
    """

    __tablename__ = "task_inputs"
    task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    # 取值约束挂在列类型上（非原生枚举 + CHECK），取值直接来自 ``InputDelivery``。
    delivery: Mapped[str] = mapped_column(
        Enum(
            *get_args(InputDelivery),
            name="task_input_delivery",
            native_enum=False,
            create_constraint=True,
            length=16,
        ),
        nullable=False,
        default="immediate",
        server_default="immediate",
    )
