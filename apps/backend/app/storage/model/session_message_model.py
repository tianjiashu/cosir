"""Session 外部消息队列：尚未并入 context 的消息；并入时删除本行。"""

from typing import Any, get_args

from sqlalchemy import JSON, Boolean, Enum, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.core.workflow.message_queue.session_message import Delivery
from app.models.message_context.message_metadata.base.message_kinds import SessionMessageKind
from app.storage.model.base import StorageBase


class SessionMessageModel(StorageBase):
    """``session_messages`` 表：Session 的外部消息队列，按 ``id`` 先进先出。

    行身份（``StorageBase.id``）就是消息的稳定身份：排队投影、立即发送与删除的寻址、并入 context 后的
    消息 id 都用它，从不更换（立即发送原地改 ``delivery``）。``payload`` 是按 ``kind`` 判别的消息 body；
    ``delivery`` 与 ``project_to_transport`` 是消息属性，单独成列便于按 SQL 过滤。
    ``interrupted_work`` 记这条用户输入是否已让本 Session 的在途工作让位
    （见 ``SessionMessageQueue.mark_interrupted_work``）：撤回时据此决定要不要告诉模型。
    """

    __tablename__ = "session_messages"
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    # 取值约束挂在列类型上（非原生枚举 + CHECK）。
    kind: Mapped[str] = mapped_column(
        Enum(
            *get_args(SessionMessageKind),
            name="session_message_kind",
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
            name="session_message_delivery",
            native_enum=False,
            create_constraint=True,
            length=16,
        ),
        nullable=False,
    )
    project_to_transport: Mapped[bool] = mapped_column(Boolean, nullable=False)
    interrupted_work: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
