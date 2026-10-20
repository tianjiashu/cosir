"""Task 待消费用户输入；消费后正文转入 context 并删除本行。"""

from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class TaskInputModel(StorageBase):
    """``task_inputs`` 表：尚未被模型消费的用户输入。

    行身份（``StorageBase.id``）就是这条用户消息的稳定身份：队列投影、编辑定位与消费进
    context 后的消息 id 都用它，不再另存一列 UUID。库内为整数主键，跨进程边界的字符串形式
    由 service 层统一渲染。
    """

    __tablename__ = "task_inputs"
    task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
