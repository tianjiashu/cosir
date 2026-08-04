"""``file_snapshots`` 表 ORM 模型（Turn 回退文件快照）。

单一职责：承载回退所需的文件操作反向 V4A 序列化快照，仅描述表结构。
CRUD 收口在 ``app.storage.crud.file_snapshot_crud``，值对象在
``app.models.file_snapshot_record``。
"""

from sqlalchemy import Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class FileSnapshotModel(StorageBase):
    """``file_snapshots`` 表：记录某 turn 触碰过的文件操作反向快照。

    每条记录对应一次文件写/改/删操作的反向 V4A 操作（已序列化的
    ``PatchOperation``），回退时按 ``seq`` 逆序 apply 即还原该 turn 的磁盘副作用。
    """

    __tablename__ = "file_snapshots"
    __table_args__ = (Index("idx_file_snapshots_turn_seq", "turn_id", "seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    turn_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool_call_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    op_json: Mapped[str] = mapped_column(Text, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
