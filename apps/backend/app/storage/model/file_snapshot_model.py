"""``file_snapshots`` 表 ORM 模型（Turn 回退文件快照）。

单一职责：承载回退所需的文件操作反向 V4A 序列化快照，仅描述表结构。
CRUD 收口在 ``app.storage.crud.file_snapshot_crud``，值对象在
``app.models.file_snapshot_record``。
"""

from sqlalchemy import Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class FileSnapshotModel(StorageBase):
    """``file_snapshots`` 表：记录某 turn 触碰过的文件操作反向快照。

    每条记录对应一次文件写/改/删操作的反向 V4A 操作（已序列化的
    ``PatchOperation``），回退时按 ``seq`` 逆序 apply 即还原该 turn 的磁盘副作用。

    ``stable`` 标记该变更是否已稳定：所属 turn 运行中落库为 0（不展示、不可撤销），
    turn 结束时置 1 才进入 task 级变更集。``status`` 为用户对该文件最新变更的处理态，
    取 ``pending`` / ``kept`` / ``reverted`` 三态之一。
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
    # 该次变更相对上一次的 diff 增删行数（基于采集层 before/after 用 difflib 统计）。
    # 用于变更集行内展示「+N -M」徽标；MOVE 计 0/0。
    additions: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    deletions: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # 变更是否已稳定：所属 turn 结束时置 1，运行中落库为 0（运行中不展示、不可撤销）。
    stable: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # 用户对该文件最新变更的处理态：pending / kept / reverted。
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default=text("'pending'")
    )
    # 撤销时间（仅 status=reverted 时有值），用于排查；非 reverted 时为空字符串。
    reverted_at: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
