"""变更集查询聚合（只读）。

单一职责：把 ``file_snapshots`` 中已稳定/运行中的反向 V4A 快照聚合为 task 维度
的变更集视图，并解析检查点截断所需信息。只读、不写库、不触碰磁盘。

设计边界：
- 不承载 keep/revert 操作（见 ``operations``），只提供查询入口与私有查询辅助。
- ``ChangeFileEntry`` 构造统一走 ``ChangeFileEntry.from_snapshot`` 工厂。
"""

from app.models.file_snapshot_record import FileSnapshotRecord
from app.models.result.change_set import ChangeCheckpoint, ChangeFileEntry, ChangeSet
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.file_snapshot_crud import FileSnapshotCrud


def _run_ids_until(
    task_id: int | str, checkpoint_run_id: int | None
) -> tuple[list[int] | None, list[ChangeCheckpoint]]:
    """解析 task 下参与聚合的 turn 过滤集合与检查点列表。

    seq 命名空间已按 task 隔离（task 内递增），聚合查询默认按 ``task_id`` 直查即可；
    仅当指定 ``checkpoint_run_id``（截断到某 turn 为止）时才需要 turn 过滤集合。

    参数:
        task_id: 任务标识（整数主键；允许传入字符串以兼容 API 路径参数，内部收敛为 int）。
        checkpoint_run_id: 检查点轮次标识；为 None 表示不截断（聚合该 task 全部快照）。

    返回:
        二元组 ``(run_ids, checkpoints)``：前者为参与变更聚合的 turn 过滤集合（按时间升序，
        指定检查点时截断到该 turn 含，为 None 表示不过滤 turn），后者为该 task 全部检查点
        （不受截断影响，供前端下拉）。

    异常:
        ValueError: 当 ``checkpoint_run_id`` 不属于该 task 时抛出。

    副作用:
        打开主库只读查询。
    """
    task_id = int(task_id)
    turns = ConversationRunCrud().list_by_task(task_id)
    checkpoints = [
        ChangeCheckpoint(run_id=turn.id, turn_seq=index, label=f"检查点 {index}")
        for index, turn in enumerate(turns, start=1)
    ]
    if checkpoint_run_id is None:
        return None, checkpoints
    run_ids = [turn.id for turn in turns]
    if checkpoint_run_id not in run_ids:
        raise ValueError(f"checkpoint turn not in task: {checkpoint_run_id}")
    return run_ids[: run_ids.index(checkpoint_run_id) + 1], checkpoints


def query_change_set(
    task_id: int | str,
    checkpoint_run_id: str | None = None,
    include_running: bool = True,
) -> ChangeSet:
    """查询某 task 的累积文件变更集。

    同一路径多次变更按 ``seq`` 升序覆盖，最终只保留最新一条对外呈现。

    参数:
        task_id: 任务标识（整数主键；允许传入字符串以兼容 API 路径参数，内部收敛为 int）。
        checkpoint_run_id: 只聚合到该 turn（含）为止的变更；为 None 表示全部。
        include_running: 默认 True，纳入运行中（``stable=0``）的变更，用于工具
            执行中的实时展示与撤销；置 False 时只返回已稳定（``stable=1``）条目。

    返回:
        ``ChangeSet``：含检查点列表与按路径去重的文件条目（按路径字典序排列）。

    异常:
        ValueError: 当 ``checkpoint_run_id`` 不属于该 task 时抛出。

    副作用:
        打开主库只读查询。
    """
    task_id = int(task_id)
    run_ids, checkpoints = _run_ids_until(task_id, checkpoint_run_id)
    crud = FileSnapshotCrud()
    snapshots = (
        crud.list_any_by_task(task_id, run_ids)
        if include_running
        else crud.list_stable_by_task(task_id, run_ids)
    )
    latest: dict[str, ChangeFileEntry] = {}
    for snap in snapshots:
        latest[snap.path] = ChangeFileEntry.from_snapshot(snap, snap.status)
    return ChangeSet(
        task_id=task_id,
        checkpoints=checkpoints,
        files=[latest[path] for path in sorted(latest)],
    )


def _require_latest_any(task_id: int | str, path: str) -> FileSnapshotRecord:
    """取某 task 下指定路径的最新快照（含运行中 ``stable=0``）。

    用于运行中可撤销：运行时同 path 的变更可能尚未稳定，但已是该 path 的
    待撤销目标态，必须纳入查询，否则运行中撤销会漏掉最新一条。

    参数:
        task_id: 任务标识。
        path: 相对 workspace 的文件路径。

    返回:
        该路径最新的 ``FileSnapshotRecord``（不限 stable）。

    异常:
        ValueError: 当该路径没有任何变更（含运行中）时抛出（API 层映射为 404）。

    副作用:
        打开主库只读查询。
    """
    task_id = int(task_id)
    snapshot = FileSnapshotCrud().latest_any_by_path(task_id, path)
    if snapshot is None:
        raise ValueError(f"no change for path: {path}")
    return snapshot
