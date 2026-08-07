"""Task 级变更集服务（累积文件变更查询 + 单文件保留/撤销）。

单一职责：把 ``file_snapshots`` 中已稳定的反向 V4A 快照聚合成 task 维度的变更集，
并提供单文件「撤销」（把文件还原到该变更之前）与「保留」（标记已确认）两个操作入口。
不直接写 SQL、不承载模型，仅编排 storage 与工具应用逻辑。

设计约束：
- 查询默认只暴露 ``stable == 1`` 的变更；``include_running=True`` 时纳入运行中
  （``stable == 0``）条目，用于「工具执行中实时展示变更」。
- 撤销始终以「该 path 的最新一条快照（含运行中）」为目标，故运行中即可撤销。
- 同一路径多次变更按 ``seq`` 取最新一条对外呈现；撤销即对该最新条目的反向操作 apply。
- 检查点粒度为「一个 turn = 一个检查点」，用于查看「到某次对话为止」的累积变更。
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.config.logging.logger import log
from app.models.enums.event_type import EventType
from app.models.event.runtime_event import RuntimeEvent
from app.models.file_snapshot_record import FileSnapshotRecord
from app.models.payload.file_change_updated_payload import FileChangeUpdatedPayload
from app.service.depends import get_runtime_event_bus, get_workspace_service
from app.service.task.task_service import TaskService
from app.storage.crud.file_snapshot_crud import FileSnapshotCrud
from app.storage.crud.turn_crud import TurnCrud
from app.tools.tool_handler.patch.patch_apply import PatchApplyError, apply_all_with_diff
from app.tools.tool_handler.patch.patch_parser import (
    Hunk,
    HunkLine,
    OperationType,
    PatchOperation,
)
from app.tools.tool_handler.security.path_resolver import PathResolver


class ChangeSetConflictError(ValueError):
    """变更集操作因并发状态冲突而被拒绝。

    与「路径无变更」（``ValueError``，API 映射 404）区分：本异常表示资源存在但
    其处理态已被并发方改掉（CAS miss，lost update 防护），语义属并发冲突，应由
    API 层映射为 409。继承 ``ValueError`` 以保持与既有调用方（catch ValueError）
    的向后兼容。
    """


@dataclass(frozen=True)
class ChangeFileEntry:
    """变更集中的单个文件条目。

    参数:
        path: 相对 workspace 的文件路径。
        action: 变更动作，取值 ``created`` / ``modified`` / ``deleted``。
        status: 用户处理态，取值 ``pending`` / ``kept`` / ``reverted``。
        last_tool_call_id: 产生该最新变更的工具调用标识。
        last_turn_id: 产生该最新变更的轮次标识。
        additions: 该次变更的 diff 新增行数。
        deletions: 该次变更的 diff 删除行数。
    """

    path: str
    action: str
    status: str
    last_tool_call_id: str
    last_turn_id: str
    additions: int = 0
    deletions: int = 0


@dataclass(frozen=True)
class ChangeCheckpoint:
    """变更集检查点（一个 turn 对应一个检查点）。

    参数:
        turn_id: 轮次标识。
        turn_seq: 该 turn 在 task 内的顺序号，从 1 开始。
        label: 展示用标签，如 ``检查点 1``。
    """

    turn_id: str
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

    task_id: str
    checkpoints: list[ChangeCheckpoint]
    files: list[ChangeFileEntry]


def _turn_ids_until(
    task_id: str, checkpoint_turn_id: str | None
) -> tuple[list[str], list[ChangeCheckpoint]]:
    """解析 task 下参与聚合的 turn 列表与检查点列表。

    参数:
        task_id: 任务标识。
        checkpoint_turn_id: 检查点轮次标识；为 None 表示取全部 turn。

    返回:
        二元组 ``(turn_ids, checkpoints)``：前者为参与变更聚合的 turn 标识（按时间升序，
        指定检查点时截断到该 turn 含），后者为该 task 全部检查点（不受截断影响，供前端下拉）。

    异常:
        ValueError: 当 ``checkpoint_turn_id`` 不属于该 task 时抛出。

    副作用:
        打开主库只读查询。
    """
    turns = TurnCrud().list_by_task(task_id)
    checkpoints = [
        ChangeCheckpoint(turn_id=turn.turn_id, turn_seq=index, label=f"检查点 {index}")
        for index, turn in enumerate(turns, start=1)
    ]
    turn_ids = [turn.turn_id for turn in turns]
    if checkpoint_turn_id is not None:
        if checkpoint_turn_id not in turn_ids:
            raise ValueError(f"checkpoint turn not in task: {checkpoint_turn_id}")
        turn_ids = turn_ids[: turn_ids.index(checkpoint_turn_id) + 1]
    return turn_ids, checkpoints


def query_change_set(
    task_id: str,
    checkpoint_turn_id: str | None = None,
    include_running: bool = True,
) -> ChangeSet:
    """查询某 task 的累积文件变更集。

    同一路径多次变更按 ``seq`` 升序覆盖，最终只保留最新一条对外呈现。

    参数:
        task_id: 任务标识。
        checkpoint_turn_id: 只聚合到该 turn（含）为止的变更；为 None 表示全部。
        include_running: 默认 True，纳入运行中（``stable=0``）的变更，用于工具
            执行中的实时展示与撤销；置 False 时只返回已稳定（``stable=1``）条目。

    返回:
        ``ChangeSet``：含检查点列表与按路径去重的文件条目（按路径字典序排列）。

    异常:
        ValueError: 当 ``checkpoint_turn_id`` 不属于该 task 时抛出。

    副作用:
        打开主库只读查询。
    """
    turn_ids, checkpoints = _turn_ids_until(task_id, checkpoint_turn_id)
    crud = FileSnapshotCrud()
    snapshots = (
        crud.list_any_by_turns(turn_ids) if include_running else crud.list_stable_by_turns(turn_ids)
    )
    latest: dict[str, ChangeFileEntry] = {}
    for snap in snapshots:
        latest[snap.path] = ChangeFileEntry(
            path=snap.path,
            action=snap.action,
            status=snap.status,
            last_tool_call_id=snap.tool_call_id,
            last_turn_id=snap.turn_id,
            additions=snap.additions,
            deletions=snap.deletions,
        )
    return ChangeSet(
        task_id=task_id,
        checkpoints=checkpoints,
        files=[latest[path] for path in sorted(latest)],
    )


def _require_latest_any(task_id: str, path: str) -> FileSnapshotRecord:
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
    turn_ids, _ = _turn_ids_until(task_id, None)
    snapshot = FileSnapshotCrud().latest_any_by_path(turn_ids, path)
    if snapshot is None:
        raise ValueError(f"no change for path: {path}")
    return snapshot


def keep_file(task_id: str, path: str) -> ChangeFileEntry:
    """把某文件的最新变更标记为「保留」。

    仅改写展示态，不触碰磁盘。运行中的快照（``stable=0``）同样可被保留，与
    ``revert_file`` 的可见性保持一致：变更一旦实时展示出来，其上的保留 / 撤销
    两个动作就都必须可用，否则前端按钮会对运行中条目报 404。

    参数:
        task_id: 任务标识。
        path: 相对 workspace 的文件路径。

    返回:
        更新后的 ``ChangeFileEntry``（``status == "kept"``）。

    异常:
        ValueError: 当该路径没有任何变更（含运行中）时抛出（API 层映射为 404）。
        ChangeSetConflictError: 当该行 ``status`` 已被并发改态（CAS 不匹配，lost update
            防护，API 映射 409）时抛出。

    副作用:
        改写 ``file_snapshots`` 中一行的 ``status``；写一条 info 日志。
    """
    snapshot = _require_latest_any(task_id, path)
    _cas_update_status(snapshot.id, "kept", task_id, path, snapshot.turn_id)
    log.info(
        "change_set_file_kept",
        extra={
            "msg": "变更集：文件变更已标记为保留",
            "data": {"task_id": task_id, "path": path, "turn_id": snapshot.turn_id},
        },
    )
    return ChangeFileEntry(
        path=snapshot.path,
        action=snapshot.action,
        status="kept",
        last_tool_call_id=snapshot.tool_call_id,
        last_turn_id=snapshot.turn_id,
        additions=snapshot.additions,
        deletions=snapshot.deletions,
    )


async def revert_file(
    task_id: str, path: str, workspace_root: Path | None = None
) -> ChangeFileEntry:
    """撤销某文件的最新变更，把文件还原到该变更之前。

    应用 ``op_json`` 中的反向 V4A 操作；若磁盘已处于还原后状态（重复撤销），
    跳过 apply 并直接标记，保证幂等。运行中的快照（``stable=0``）也纳入查询，
    故运行中即可撤销。

    R6 软冲突防护（解法 A，零表改动）：apply 前对每个反向操作做三方态判定——
    当前磁盘等于还原目标态（before）则静默跳过；等于 Agent 改完态（after）则
    正常 apply；两者都不等说明用户手动改过文件，主动抛 ``PatchApplyError``(409)
    拒绝撤销，避免整文件覆盖抹掉用户改动。

    参数:
        task_id: 任务标识。
        path: 相对 workspace 的文件路径。
        workspace_root: 调用方注入的 workspace 根路径；为 None 时经 task→workspace 解析。

    返回:
        更新后的 ``ChangeFileEntry``（``status == "reverted"``）。

    异常:
        ValueError: 当该路径没有任何变更（含运行中）时抛出。
        PatchApplyError: 当反向操作应用失败、或检测到用户手动修改导致软冲突(409)时抛出；
            此时状态不被改写，可重试。
        ChangeSetConflictError: 当该快照 ``status`` 已被并发改态（CAS miss，lost update
            防护，API 映射 409）时抛出；此时状态字段不被改写。注意：磁盘反向操作可能已应用
            （文件已还原）——这是方案 A「先 apply 后 CAS」的固有边界；重试时因
            ``_is_already_reverted`` 判定磁盘已还原而幂等跳过 apply，仅重新尝试状态标记。

    副作用:
        修改 workspace 内文件；改写 ``file_snapshots`` 的 ``status`` 与 ``reverted_at``；
        经事件总线广播 ``FILE_CHANGE_UPDATED``；写日志。
    """
    snapshot = _require_latest_any(task_id, path)
    if workspace_root is None:
        workspace_root = _resolve_workspace_root(task_id)
    resolver = PathResolver(workspace_root)
    for operation in _snapshots_to_operations([snapshot]):
        if _is_already_reverted(operation, resolver):
            continue
        if not _is_at_after_state(operation, resolver):
            # 磁盘既非还原目标态、也非 Agent 改完态，判定为用户手动改动 → 拒绝撤销。
            log.warning(
                "change_set_revert_conflict",
                extra={
                    "msg": "变更集：文件已被手动修改，撤销被拒",
                    "data": {
                        "task_id": task_id,
                        "path": path,
                        "turn_id": snapshot.turn_id,
                        "operation": operation.operation.value,
                    },
                },
            )
            raise PatchApplyError(
                f"file manually modified, revert rejected: {path}",
                partial_applied=False,
            )
        try:
            apply_all_with_diff([operation], resolver)
        except Exception:
            log.exception(
                "change_set_revert_failed",
                extra={
                    "msg": "变更集：单文件撤销失败",
                    "data": {
                        "task_id": task_id,
                        "path": path,
                        "turn_id": snapshot.turn_id,
                        "operation": operation.operation.value,
                    },
                },
            )
            raise
    # revert 允许幂等重入：status 已是 reverted 时再次撤销可安全通过（磁盘已还原，R6 跳过）。
    # 但若被并发改为 kept 或其它状态，CAS 拒绝，避免 lost update。
    _cas_update_status(
        snapshot.id,
        "reverted",
        task_id,
        path,
        snapshot.turn_id,
        reverted_at=datetime.now(UTC).isoformat(),
        expected_statuses=("pending", "reverted"),
    )
    _publish_revert_updated(task_id, snapshot)
    log.info(
        "change_set_file_reverted",
        extra={
            "msg": "变更集：文件变更已撤销",
            "data": {"task_id": task_id, "path": path, "turn_id": snapshot.turn_id},
        },
    )
    return ChangeFileEntry(
        path=snapshot.path,
        action=snapshot.action,
        status="reverted",
        last_tool_call_id=snapshot.tool_call_id,
        last_turn_id=snapshot.turn_id,
        additions=snapshot.additions,
        deletions=snapshot.deletions,
    )


def _cas_update_status(
    snapshot_id: int,
    status: str,
    task_id: str,
    path: str,
    turn_id: str,
    reverted_at: str = "",
    expected_statuses: Sequence[str] = ("pending",),
) -> None:
    """以 CAS 语义更新快照处理态，CAS 不匹配时拒绝并抛错（lost update 防护）。

    `keep_file` / `revert_file` 先读取最新快照再改写其 ``status``，读与写之间存在
    时间窗口：若并发（如 turn 结束收口、另一路 keep/revert、同 path 新快照落库）已把该
    行 ``status`` 改掉，无条件覆盖会丢失并发方的最新状态。此处把「读到的期望值集合」作为
    CAS 条件传入单条原子 UPDATE，行数为 0 即判定并发改态，抛
    ``ChangeSetConflictError`` 拒绝而非盲写。

    参数:
        snapshot_id: 快照主键。
        status: 目标状态，取值 ``kept`` / ``reverted``。
        task_id: 所属任务标识（仅用于日志）。
        path: 文件路径（仅用于日志）。
        turn_id: 所属轮次标识（仅用于日志）。
        reverted_at: 撤销时间字符串；``status == "reverted"`` 时传入，否则空串。
        expected_statuses: 允许的当前状态集合（CAS 条件）。``revert_file`` 允许
            ``("pending", "reverted")`` 以支持幂等重入；``keep_file`` 仅 ``("pending",)``。

    返回:
        无。

    异常:
        ChangeSetConflictError: 当该行当前 ``status`` 不属于 ``expected_statuses``
            （CAS 不匹配）时抛出，表示存在并发改态，调用方应视为冲突（API 映射 409）
            而非覆盖。

    副作用:
        写入一条 warning 日志；可能改写 ``file_snapshots`` 中一行的 ``status``。
    """
    affected = FileSnapshotCrud().update_status(
        snapshot_id, status, reverted_at=reverted_at, expected_statuses=expected_statuses
    )
    if affected == 0:
        log.warning(
            "change_set_status_cas_miss",
            extra={
                "msg": "变更集：快照处理态已被并发改态，操作被拒（lost update 防护）",
                "data": {"task_id": task_id, "path": path, "turn_id": turn_id, "target": status},
            },
        )
        raise ChangeSetConflictError(f"change status already mutated, operation rejected: {path}")


def _is_already_reverted(operation: PatchOperation, resolver: PathResolver) -> bool:
    """判断单个反向操作是否已在上一次（被中断的）回退中完成，可安全跳过。

    参数:
        operation: 反向 PatchOperation（ADD/DELETE/UPDATE/MOVE）。
        resolver: workspace 路径解析器。

    返回:
        True 表示该操作目标态已达成（重入时应跳过）；False 表示仍需 apply。
    """
    resolved, err = resolver.resolve_within_workspace(operation.file_path)
    if resolved is None or err:
        return False
    if operation.operation == OperationType.ADD:
        if not Path(resolved).exists():
            return False
        expected = (
            operation.content
            if operation.content is not None
            else _hunk_to_content_patch(operation)
        )
        try:
            # 用 utf-8-sig 读取以剥离磁盘字节中的 BOM 前缀；同时对 ``expected``
            # 剥离文本首字符 \ufeff（delete 采集的 before 以 utf-8 解码保留了该
            # 字符，而 apply 重建文件写入的正是 \ufeff），使两侧可比。否则含 BOM
            # 文件重入时会因 ``current`` 无 \ufeff 而判定未还原，导致重复 apply
            # 触发 destination-already-exists 卡死。
            current = Path(resolved).read_bytes().decode("utf-8-sig")
            return current == _strip_leading_bom(expected)
        except OSError:
            return False
    elif operation.operation == OperationType.DELETE:
        return not Path(resolved).exists()
    elif operation.operation == OperationType.UPDATE:
        if operation.content is None or not Path(resolved).exists():
            return False
        try:
            current = Path(resolved).read_bytes().decode("utf-8-sig")
            return current == _strip_leading_bom(operation.content)
        except OSError:
            return False
    else:  # OperationType.MOVE：文件应已移到 new_path 且源不存在
        dst, dst_err = resolver.resolve_within_workspace(operation.new_path or "")
        if dst is None or dst_err or not Path(dst).exists():
            return False
        return not Path(resolved).exists()


def _is_at_after_state(operation: PatchOperation, resolver: PathResolver) -> bool:
    """判断磁盘当前是否处于「Agent 改完态（after）」，即反向操作本应撤销掉的内容。

    R6 软冲突防护（解法 A）的对称判定：与 ``_is_already_reverted`` 互补——
    前者判定「已还原到 before」，本函数判定「仍是 Agent 改完的 after」。
    只有当两者都为 False 时，才说明用户手动改过文件（既非 before 也非 after）。

    反向操作语义映射（op_json 存的是**反向** PatchOperation，故 after 态与
    ``_is_already_reverted`` 判定的 before 态互为镜像，不可照搬其分支）：
    - 反向 ADD（原操作为 DELETE：Agent 删掉了文件，撤销需重建）：
      after = 文件**不存在**（Agent 删完的状态）。
    - 反向 DELETE（原操作为 ADD：Agent 新建了文件，撤销需删除）：
      after = 文件**存在**（Agent 建完的状态）。内容不做比对：新建后再被后续工具
      修改仍属 Agent 改动范畴，此处只防「用户手动删除/重建」造成的误覆盖。
    - 反向 UPDATE（整文件覆盖回 before）：after = 文件内容等于反向 op 的 ``-`` 行
      拼出的内容（即原操作的 ``+`` 行 = Agent 改完态）。
    - 反向 MOVE：after = 已移到 new_path 且源不存在。

    参数:
        operation: 反向 PatchOperation（ADD/DELETE/UPDATE/MOVE）。
        resolver: workspace 路径解析器。

    返回:
        True 表示磁盘处于 Agent 改完态（应正常 apply 撤销）；False 表示不是。
    """
    resolved, err = resolver.resolve_within_workspace(operation.file_path)
    if resolved is None or err:
        return False
    if operation.operation == OperationType.ADD:
        # 反向 ADD 对应原 DELETE：Agent 改完态就是「文件已被删除」。
        return not Path(resolved).exists()
    elif operation.operation == OperationType.DELETE:
        # 反向 DELETE 对应原 ADD：Agent 改完态就是「文件已存在」。
        return Path(resolved).exists()
    elif operation.operation == OperationType.UPDATE:
        # 注意：after 态只由反向 op 的 ``-`` 行还原，与 ``operation.content``（before 态）
        # 无关，故此处不得用 content is None 提前返回，否则无 content 的 UPDATE 反向 op
        # 会被误判为冲突。
        if not Path(resolved).exists():
            return False
        after_content = _hunk_minus_to_content(operation)
        if not after_content:
            # 反向 op 无可用 ``-`` 行（极端采集缺漏），退化为「非 after 态」交由
            # 上层按冲突处理，避免误判放行覆盖用户文件。
            return False
        try:
            current = Path(resolved).read_bytes().decode("utf-8-sig")
        except OSError:
            return False
        # ``-`` 行是按行拼接的，结构上无法还原原文件的尾换行；直接全等比较会把
        # "a\nb\n" 误判成与 "a\nb" 不同，导致几乎所有正常撤销都被拒。故在尾换行
        # 维度做归一化比较。
        # 已知取舍：用户「只增删尾部空行」这一种改动不会被 R6 判定为冲突，撤销会
        # 连同该尾换行一起还原。相较「正常撤销大面积失效」，此代价可接受。
        return _strip_leading_bom(current).rstrip("\r\n") == _strip_leading_bom(
            after_content
        ).rstrip("\r\n")
    else:  # OperationType.MOVE：目标已移到 new_path 且源不存在
        dst, dst_err = resolver.resolve_within_workspace(operation.new_path or "")
        if dst is None or dst_err or not Path(dst).exists():
            return False
        return not Path(resolved).exists()


def _hunk_minus_to_content(operation: PatchOperation) -> str:
    """从反向 UPDATE 操作的 hunks 拼接 '-' 行内容（原操作改完态的降级还原文本）。

    反向 UPDATE 的 ``-`` 行对应原操作的 ``+`` 行，即 Agent 把文件改成的样子（after 态）。
    当反向 op 未携带完整 ``content`` 时，用此拼出 after 内容供 R6 冲突比对。

    参数:
        operation: 反向 UPDATE 操作（依赖其 hunks）。

    返回:
        由 hunks 中 '-' 行内容以换行拼接的文本；无 '-' 行时返回空串。
    """
    return "\n".join(
        line.content for hunk in operation.hunks for line in hunk.lines if line.prefix == "-"
    )


def _publish_revert_updated(task_id: str, snapshot: FileSnapshotRecord) -> None:
    """撤销成功后广播 ``FILE_CHANGE_UPDATED``，驱动前端实时把该条目从列表中移除。

    参数:
        task_id: 任务标识。
        snapshot: 刚被撤销的快照记录（提供 turn_id/path/action）。

    返回:
        无。

    异常:
        无（总线异常被吞，避免阻断主流程；见下方说明）。

    副作用:
        经 ``get_runtime_event_bus()`` 单例发布一条不持久化的实时事件。
    """
    try:
        get_runtime_event_bus().publish(
            RuntimeEvent(
                event_type=EventType.FILE_CHANGE_UPDATED,
                task_id=task_id,
                turn_id=snapshot.turn_id,
                payload=FileChangeUpdatedPayload(
                    task_id=task_id,
                    turn_id=snapshot.turn_id,
                    path=snapshot.path,
                    action=snapshot.action,
                ),
            )
        )
    except Exception:
        # 总线为运行时增强能力，广播失败不得影响撤销主流程；记完整堆栈备查。
        log.exception(
            "change_set_revert_publish_failed",
            extra={
                "msg": "变更集：撤销后实时广播失败（不影响磁盘与状态）",
                "data": {
                    "task_id": task_id,
                    "path": snapshot.path,
                    "turn_id": snapshot.turn_id,
                },
            },
        )


def _hunk_to_content_patch(operation: PatchOperation) -> str:
    """从 ADD 操作的 hunks 拼接 '+' 行内容（content 字段缺失时的降级还原文本）。

    与 ``_hunk_minus_to_content`` 对称：反向 ADD 的 ``+`` 行即待重建的文件内容。

    参数:
        operation: 反向 ADD 操作（content 为 None 时回退到 hunks 拼接）。

    返回:
        由 hunks 中 '+' 行内容以换行拼接的文本（不保留原始尾换行，仅防御性降级）。

    异常:
        无。

    副作用:
        无。
    """
    return "\n".join(
        line.content for hunk in operation.hunks for line in hunk.lines if line.prefix == "+"
    )


def _strip_leading_bom(content: str) -> str:
    """剥离文本开头的单个 U+FEFF BOM 标记，使与 ``utf-8-sig`` 解码结果可比。

    参数:
        content: 待比较的文本内容。

    返回:
        去掉开头单个 ``\\ufeff`` 后的文本；无 BOM 标记时原样返回。

    异常:
        无。

    副作用:
        无。
    """
    return content[1:] if content.startswith("\ufeff") else content


def _snapshots_to_operations(
    snapshots: list[FileSnapshotRecord],
) -> list[PatchOperation]:
    """把快照记录反序列化为反向 PatchOperation 列表（已按 seq 降序）。

    参数:
        snapshots: 已按 seq 降序的快照记录。

    返回:
        可直接喂给 ``apply_all_with_diff`` 的反向 PatchOperation 列表。
    """
    operations: list[PatchOperation] = []
    for snap in snapshots:
        data = json.loads(snap.op_json)
        # 序列化时 operation 已落为 value 字符串、hunks 展开为 {"lines":[...]}，
        # 此处重建回 PatchOperation 的 Enum 与嵌套 dataclass 结构。
        hunks = [
            Hunk(
                lines=[
                    HunkLine(prefix=line["prefix"], content=line["content"])
                    for line in hunk["lines"]
                ]
            )
            for hunk in data.get("hunks", [])
        ]
        operations.append(
            PatchOperation(
                operation=OperationType(data["operation"]),
                file_path=data["file_path"],
                new_path=data.get("new_path"),
                hunks=hunks,
                content=data.get("content"),
            )
        )
    return operations


def _resolve_workspace_root(task_id: str) -> Path:
    """解析任务所属 workspace 的根路径（兜底路径）。

    参数:
        task_id: 任务标识。

    返回:
        workspace 根目录的 :class:`Path`。

    异常:
        KeyError: 当 task 或 workspace 不存在时抛出。

    副作用:
        无（只读）。
    """
    task = TaskService().get_task(task_id)
    workspace = get_workspace_service().get_workspace(task.workspace_id)
    return Path(workspace.root_path)
