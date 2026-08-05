"""Task 级变更集服务（累积文件变更查询 + 单文件保留/撤销）。

单一职责：把 ``file_snapshots`` 中已稳定的反向 V4A 快照聚合成 task 维度的变更集，
并提供单文件「撤销」（把文件还原到该变更之前）与「保留」（标记已确认）两个操作入口。
不直接写 SQL、不承载模型，仅编排 storage 与工具应用逻辑。

设计约束：
- 只暴露 ``stable == 1`` 的变更：运行中的工具调用尚未定稿，不可展示、不可撤销。
- 同一路径多次变更按 ``seq`` 取最新一条对外呈现；撤销即对该最新条目的反向操作 apply。
- 检查点粒度为「一个 turn = 一个检查点」，用于查看「到某次对话为止」的累积变更。
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.config.logging.logger import log
from app.models.file_snapshot_record import FileSnapshotRecord
from app.service.depends import get_workspace_service
from app.service.task.task_service import TaskService
from app.storage.crud.file_snapshot_crud import FileSnapshotCrud
from app.storage.crud.turn_crud import TurnCrud
from app.tools.tool_handler.patch.patch_apply import apply_all_with_diff
from app.tools.tool_handler.patch.patch_parser import (
    Hunk,
    HunkLine,
    OperationType,
    PatchOperation,
)
from app.tools.tool_handler.security.project_path import ProjectPathResolver


@dataclass(frozen=True)
class ChangeFileEntry:
    """变更集中的单个文件条目。

    参数:
        path: 相对 workspace 的文件路径。
        action: 变更动作，取值 ``created`` / ``modified`` / ``deleted``。
        status: 用户处理态，取值 ``pending`` / ``kept`` / ``reverted``。
        last_tool_call_id: 产生该最新变更的工具调用标识。
        last_turn_id: 产生该最新变更的轮次标识。
    """

    path: str
    action: str
    status: str
    last_tool_call_id: str
    last_turn_id: str


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


def query_change_set(task_id: str, checkpoint_turn_id: str | None = None) -> ChangeSet:
    """查询某 task 的累积文件变更集（仅已稳定条目）。

    同一路径多次变更按 ``seq`` 升序覆盖，最终只保留最新一条对外呈现。

    参数:
        task_id: 任务标识。
        checkpoint_turn_id: 只聚合到该 turn（含）为止的变更；为 None 表示全部。

    返回:
        ``ChangeSet``：含检查点列表与按路径去重的文件条目（按路径字典序排列）。

    异常:
        ValueError: 当 ``checkpoint_turn_id`` 不属于该 task 时抛出。

    副作用:
        打开主库只读查询。
    """
    turn_ids, checkpoints = _turn_ids_until(task_id, checkpoint_turn_id)
    latest: dict[str, ChangeFileEntry] = {}
    for snap in FileSnapshotCrud().list_stable_by_turns(turn_ids):
        latest[snap.path] = ChangeFileEntry(
            path=snap.path,
            action=snap.action,
            status=snap.status,
            last_tool_call_id=snap.tool_call_id,
            last_turn_id=snap.turn_id,
        )
    return ChangeSet(
        task_id=task_id,
        checkpoints=checkpoints,
        files=[latest[path] for path in sorted(latest)],
    )


def _require_latest_stable(task_id: str, path: str) -> FileSnapshotRecord:
    """取某 task 下指定路径的最新已稳定快照，缺失时报错。

    参数:
        task_id: 任务标识。
        path: 相对 workspace 的文件路径。

    返回:
        该路径最新的已稳定 ``FileSnapshotRecord``。

    异常:
        ValueError: 当该路径没有已稳定变更时抛出（API 层映射为 404）。

    副作用:
        打开主库只读查询。
    """
    turn_ids, _ = _turn_ids_until(task_id, None)
    snapshot = FileSnapshotCrud().latest_stable_by_path(turn_ids, path)
    if snapshot is None:
        raise ValueError(f"no stable change for path: {path}")
    return snapshot


def keep_file(task_id: str, path: str) -> ChangeFileEntry:
    """把某文件的最新变更标记为「保留」。

    仅改写展示态，不触碰磁盘。

    参数:
        task_id: 任务标识。
        path: 相对 workspace 的文件路径。

    返回:
        更新后的 ``ChangeFileEntry``（``status == "kept"``）。

    异常:
        ValueError: 当该路径没有已稳定变更时抛出。

    副作用:
        改写 ``file_snapshots`` 中一行的 ``status``；写一条 info 日志。
    """
    snapshot = _require_latest_stable(task_id, path)
    FileSnapshotCrud().update_status(snapshot.id, "kept")
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
    )


async def revert_file(
    task_id: str, path: str, workspace_root: Path | None = None
) -> ChangeFileEntry:
    """撤销某文件的最新变更，把文件还原到该变更之前。

    应用 ``op_json`` 中的反向 V4A 操作；若磁盘已处于还原后状态（重复撤销），
    跳过 apply 并直接标记，保证幂等。

    参数:
        task_id: 任务标识。
        path: 相对 workspace 的文件路径。
        workspace_root: 调用方注入的 workspace 根路径；为 None 时经 task→workspace 解析。

    返回:
        更新后的 ``ChangeFileEntry``（``status == "reverted"``）。

    异常:
        ValueError: 当该路径没有已稳定变更时抛出。
        PatchApplyError: 当反向操作应用失败时向上抛出，此时状态不被改写，可重试。

    副作用:
        修改 workspace 内文件；改写 ``file_snapshots`` 的 ``status`` 与 ``reverted_at``；写日志。
    """
    snapshot = _require_latest_stable(task_id, path)
    if workspace_root is None:
        workspace_root = _resolve_workspace_root(task_id)
    resolver = ProjectPathResolver(workspace_root)
    for operation in _snapshots_to_operations([snapshot]):
        if _is_already_reverted(operation, resolver):
            continue
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
    FileSnapshotCrud().update_status(
        snapshot.id, "reverted", reverted_at=datetime.now(UTC).isoformat()
    )
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
    )


def _is_already_reverted(operation: PatchOperation, resolver: ProjectPathResolver) -> bool:
    """判断单个反向操作是否已在上一次（被中断的）回退中完成，可安全跳过。

    参数:
        operation: 反向 PatchOperation（ADD/DELETE/UPDATE/MOVE）。
        resolver: workspace 路径解析器。

    返回:
        True 表示该操作目标态已达成（重入时应跳过）；False 表示仍需 apply。
    """
    resolved, err = resolver.resolve(operation.file_path)
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
        dst, dst_err = resolver.resolve(operation.new_path or "")
        if dst is None or dst_err or not Path(dst).exists():
            return False
        return not Path(resolved).exists()


def _hunk_to_content_patch(operation: PatchOperation) -> str:
    """从 ADD 操作的 hunks 拼接 '+' 行内容（content 字段缺失时的降级还原文本）。

    复用 ``v4a_reverse._hunk_to_content`` 的同一语义，避免两处拼接逻辑漂移。

    参数:
        operation: 反向 ADD 操作（content 为 None 时回退到 hunks 拼接）。

    返回:
        由 hunks 中 '+' 行内容以换行拼接的文本（不保留原始尾换行，仅防御性降级）。

    异常:
        无。

    副作用:
        无。
    """
    return _hunk_to_content_patch_impl(operation)


def _hunk_to_content_patch_impl(operation: PatchOperation) -> str:
    """从操作的 hunks 拼接 '+' 行内容（content 字段缺失时的降级还原文本）。

    参数:
        operation: 反向 ADD 操作（content 为 None 时回退到 hunks 拼接）。

    返回:
        由 hunks 中 '+' 行内容以换行拼接的文本。

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
