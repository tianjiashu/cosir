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

    ``seq`` 为 **task 内**递增序号：同一 task 下按 turn 执行序单调递增，不同 task
    各自从 0 开始互不共享命名空间（task 是并发边界）。``task_id`` 列如实表达快照
    归属；``uq_file_snapshots_task_seq`` 唯一索引作为防御保险丝，防止未来误用把
    seq 命名空间打破导致跨 task/turn 排序错乱。``idx_file_snapshots_turn_seq`` 供
    按单 turn 回放查询使用。
    """

    __tablename__ = "file_snapshots"
    __table_args__ = (
        # 单 turn 回放（list_by_turn）：turn_id + seq 升序。
        Index("idx_file_snapshots_turn_seq", "turn_id", "seq"),
        # task 聚合查询（list_*_by_task）与 latest_by_path：task_id + path + seq。
        Index("idx_file_snapshots_task_path_seq", "task_id", "path", "seq"),
        # 保险丝：task 内 seq 必须唯一（seq 命名空间 = task）。
        Index("uq_file_snapshots_task_seq", "task_id", "seq", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 快照归属任务：seq 的命名空间与并发边界，迁移回填见 init_schema。
    task_id: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
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
