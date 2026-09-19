"""``file_snapshots`` 表 ORM 模型（Task 文件变更集快照）。

单一职责：承载文件变更的前后状态快照和 ChangeSet 生命周期，仅描述表结构。
CRUD 收口在 ``app.storage.crud.file_snapshot_crud``，值对象在
``app.models.file_snapshot_record``。
"""

from sqlalchemy import ForeignKey, Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class FileSnapshotModel(StorageBase):
    """``file_snapshots`` 表：记录某 Run 对工作区文件造成的变更。

    已收口记录包含一项或多项路径的 before/after 状态；before-image 内容由本地 blob
    存储承载。预写期间每个触碰路径先占一条隐藏记录，文件操作后按观察到的实际状态
    收口为 ChangeSet。Revert 使用同一记录上的临时状态标记，进程重启后只按当前磁盘
    状态修正记录，不自动还原文件。

    Run 终态以 ``conversation_runs.status`` 为准，不在快照表中复制。``status`` 为用户
    对该文件最新变更的处理态，取 ``pending`` / ``kept`` / ``reverted`` 三态之一。

    ``seq`` 为 **task 内**递增序号：同一 task 下按 turn 执行序单调递增，不同 task
    各自从 0 开始互不共享命名空间（task 是并发边界）。``task_id`` 列如实表达快照
    归属；``uq_file_snapshots_task_seq`` 唯一索引作为防御保险丝，防止未来误用把
    seq 命名空间打破导致跨 task/turn 排序错乱。``idx_file_snapshots_turn_seq`` 供
    按单 turn 回放查询使用。
    """

    __tablename__ = "file_snapshots"
    __table_args__ = (
        # 单 turn 回放（list_by_turn）：run_id + seq 升序。
        Index("idx_file_snapshots_turn_seq", "run_id", "seq"),
        # task 聚合查询与按路径查询：task_id + path + seq。
        Index("idx_file_snapshots_task_path_seq", "task_id", "path", "seq"),
        Index("idx_file_snapshots_mutation", "mutation_id", "mutation_state"),
        # 保险丝：task 内 seq 必须唯一（seq 命名空间 = task）。
        Index("uq_file_snapshots_task_seq", "task_id", "seq", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 快照归属任务：seq 的命名空间与并发边界。
    task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tasks.id"), nullable=False, default=0, server_default=text("0")
    )
    run_id: Mapped[int] = mapped_column(Integer, ForeignKey("conversation_runs.id"), nullable=False)
    tool_call_id: Mapped[str] = mapped_column(Text, nullable=False)
    # 文件操作结果尚未确定时，一次工作区变更可以为多个路径预留记录。
    mutation_id: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    # prepared/reverting 表示变更尚在执行或回退中，不作为已收口 ChangeSet 返回。
    mutation_state: Mapped[str] = mapped_column(
        Text, nullable=False, default="applied", server_default=text("'applied'")
    )
    # 仅 prepared 操作需要持久化恢复清单；收口或回退后清空。
    mutation_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    # 该记录代表的工作区相对路径；保留为列以支持按路径查询与索引。
    path: Mapped[str] = mapped_column(Text, nullable=False)
    # ChangeSet 规范化 JSON 信封，包含操作元数据及路径前后状态。
    op_json: Mapped[str] = mapped_column(Text, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # 用户对该文件最新变更的处理态：pending / kept / reverted。
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default=text("'pending'")
    )
