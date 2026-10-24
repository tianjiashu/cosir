"""Task 外部消息队列：尚未并入 context 的消息；并入时删除本行。"""

from typing import Any, get_args

from sqlalchemy import JSON, Boolean, Enum, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.models.message_metadata import MessageKind
from app.models.task_message import Delivery
from app.storage.model.base import StorageBase


class TaskMessageModel(StorageBase):
    """``task_messages`` 表：Task 的外部消息队列，按 ``id`` 先进先出。

    行身份（``StorageBase.id``）就是消息的稳定身份：排队投影、编辑定位与并入 context 后的消息 id
    都用它。``payload`` 是按 ``kind`` 判别的消息 body；``delivery`` 与 ``project_to_transport`` 是
    消息属性，单独成列便于按 SQL 过滤。
    """

    __tablename__ = "task_messages"
    task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    # 取值约束挂在列类型上（非原生枚举 + CHECK）。
    kind: Mapped[str] = mapped_column(
        Enum(
            *get_args(MessageKind),
            name="task_message_kind",
            native_enum=False,
            create_constraint=True,
            length=32,
        ),
        nullable=False,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    delivery: Mapped[str] = mapped_column(
        Enum(
            *get_args(Delivery),
            name="task_message_delivery",
            native_enum=False,
            create_constraint=True,
            length=16,
        ),
        nullable=False,
    )
    project_to_transport: Mapped[bool] = mapped_column(Boolean, nullable=False)
