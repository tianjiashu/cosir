"""Task 级变更集聚合结果值对象。

把 file_snapshots 快照聚合后的对外视图（检查点 + 按路径去重的文件条目）表达为
不可变值对象，供 change_set 服务层与 API 响应层共用。不承载任何查询或业务逻辑，
仅提供从快照记录构造条目的工厂方法。

设计边界：
- 不依赖 service/storage/tools，只引用 ``FileSnapshotRecord`` 字段（models 层内部引用）。
- 与 ``ChangeSetResponse`` 的分工：本文件是领域值对象，API 响应模型负责投影。
"""

from dataclasses import dataclass

from app.models.file_snapshot_record import FileSnapshotRecord


@dataclass(frozen=True)
class ChangeFileEntry:
    """变更集中的单个文件条目。

    参数:
        path: 相对 workspace 的文件路径。
        action: 变更动作，取值 ``created`` / ``modified`` / ``deleted``。
        status: 用户处理态，取值 ``pending`` / ``kept`` / ``reverted``。
        last_tool_call_id: 产生该最新变更的工具调用标识。
        last_run_id: 产生该最新变更的轮次标识（int，与 ``FileSnapshotRecord.run_id`` 同维度）。
        additions: 该次变更的 diff 新增行数。
        deletions: 该次变更的 diff 删除行数。
    """

    path: str
    action: str
    status: str
    last_tool_call_id: str
    last_run_id: int
    additions: int = 0
    deletions: int = 0

    @classmethod
    def from_snapshot(cls, snapshot: FileSnapshotRecord, status: str) -> "ChangeFileEntry":
        """从快照记录构造变更文件条目（统一「快照 + 处理态 → 条目」的字段映射）。

        ``query_change_set`` / ``keep_file`` / ``revert_file`` 三处曾各自重复
        逐字段构造，Rule of Three 触发后收口为本工厂方法，仅 ``status`` 由调用方
        指定（快照自身状态 / ``kept`` / ``reverted``）。

        参数:
            snapshot: 快照记录（提供 path/action/tool_call_id/run_id/additions/deletions）。
            status: 目标处理态，取值 ``pending`` / ``kept`` / ``reverted``。

        返回:
            与快照字段一一对应的 ``ChangeFileEntry``。

        异常:
            无。

        副作用:
            无。
        """
        return cls(
            path=snapshot.path,
            action=snapshot.action,
            status=status,
            last_tool_call_id=snapshot.tool_call_id,
            last_run_id=snapshot.run_id,
            additions=snapshot.additions,
            deletions=snapshot.deletions,
        )


@dataclass(frozen=True)
class ChangeCheckpoint:
    """变更集检查点（一个 turn 对应一个检查点）。

    参数:
        run_id: 轮次标识。
        turn_seq: 该 turn 在 task 内的顺序号，从 1 开始。
        label: 展示用标签，如 ``检查点 1``。
    """

    run_id: int
    turn_seq: int
    label: str


@dataclass(frozen=True)
class ChangeSet:
    """某 task 的累积变更集。

    参数:
        task_id: 任务标识。
        checkpoints: 该 task 下按时间升序的检查点列表。
        files: 按路径去重后的文件条目列表（每个路径保留最新一条变更）。
    """

    task_id: int
    checkpoints: list[ChangeCheckpoint]
    files: list[ChangeFileEntry]
