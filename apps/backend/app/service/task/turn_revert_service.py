"""Turn 回退编排服务（原地撤销最新已结束 turn）。

单一职责：把「文件快照还原 + 对话轨迹清空 + 审计事件」三步编排成 ``revert_turn``
单一入口，供 API 层调用；不直接写 SQL、不承载模型，仅编排 storage 与工具应用逻辑。

设计约束（方案 §二 D6）：
- 第一版仅支持回退「序列中最新一个已结束 turn」；中间历史 turn 因会造成上下文悬空、
  同文件后续改动被覆盖等撕裂态，第一版不开放（见 §十.11）。
- 回退遵循「先还原文件后清空状态」的两段式（§六），且文件反向操作按 seq 降序应用，
  确保同路径多次操作（write→patch→delete）逆序还原后磁盘 == 初始状态。该顺序保证
  还原失败时不丢快照、turn 保持原状可重入续跑。
- 可重入：任一步骤失败后重入 ``revert_turn`` 应能继续完成（快照重复 apply 幂等、
  清空步骤幂等）。
"""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.config.logging.logger import log
from app.models.enums.turn_status import TurnStatus
from app.models.file_snapshot_record import FileSnapshotRecord
from app.service.depends import get_workspace_service
from app.service.task.task_service import TaskService
from app.storage.crud.file_snapshot_crud import FileSnapshotCrud
from app.storage.crud.runtime_event_crud import RuntimeEventCrud
from app.storage.crud.turn_crud import TurnCrud
from app.storage.crud.turn_message_crud import TurnMessageCrud
from app.storage.store_engines import checkpoint_path
from app.tools.tool_handler.patch.patch_apply import apply_all_with_diff
from app.tools.tool_handler.patch.patch_parser import (
    Hunk,
    HunkLine,
    OperationType,
    PatchOperation,
)
from app.tools.tool_handler.security.project_path import ProjectPathResolver
from app.trace_infra.ids import new_event_id

_FINISHED_STATUSES = frozenset(
    {
        TurnStatus.COMPLETED.value,
        TurnStatus.FAILED.value,
        TurnStatus.CANCELLED.value,
        TurnStatus.REVERTED.value,
    }
)

# 按 turn_id 分桶的并发锁：保证同一 turn 不会被两个协程同时回退。
_revert_locks: dict[str, asyncio.Lock] = {}
_revert_locks_guard = asyncio.Lock()


@dataclass
class RevertResult:
    """Turn 回退结果值对象。

    参数:
        ok: 回退是否成功。
        turn_id: 被回退的轮次标识。
        file_reverted: 是否成功还原了文件（有快照且 apply 成功）。
        non_revertible_actions: 当前 turn 含的不可靠文件快照还原的动作名列表
            （如 execute_terminal），供前端提示。
        conflict_subsequent_turns: 保留字段（第一版恒为 False）；历史 turn 回退
            开放后用于承载「该 turn 之后还有已结束 turn」的冲突提示。
    """

    ok: bool
    turn_id: str
    file_reverted: bool
    non_revertible_actions: list[str]
    conflict_subsequent_turns: bool = False


async def _acquire_lock(turn_id: str) -> asyncio.Lock:
    """按 turn_id 取得（或创建）并发锁。

    参数:
        turn_id: 目标轮次标识。

    返回:
        该 turn 专属的 asyncio.Lock。
    """
    async with _revert_locks_guard:
        lock = _revert_locks.get(turn_id)
        if lock is None:
            lock = asyncio.Lock()
            _revert_locks[turn_id] = lock
        return lock


def _is_latest_finished_turn(turn_crud: TurnCrud, turn_id: str) -> bool:
    """判断 turn 是否为其 task 序列中最新一个已结束 turn（D6 守卫依据）。

    参数:
        turn_crud: turn 表 CRUD。
        turn_id: 待校验的轮次标识。

    返回:
        True 表示该 turn 已结束且其 task 下不存在比它更晚创建的已结束 turn；
        False 表示其为中间历史 turn 或根本不是已结束 turn（后者由调用方先校验）。
    """
    turn = turn_crud.get(turn_id)
    # list_by_task 已按 (created_at, turn_id) 稳定升序，最后一个即「最新」。
    # 直接取末尾，避免对同秒创建 turn 用字典序 max 造成的不确定判定（MA-2）。
    task_turns = turn_crud.list_by_task(turn.task_id)
    finished_turns = [t for t in task_turns if t.status in _FINISHED_STATUSES]
    if not finished_turns:
        return False
    return finished_turns[-1].turn_id == turn_id


async def revert_turn(turn_id: str, workspace_root: Path | None = None) -> RevertResult:
    """原地回退一个已结束 turn（D6：仅最新已结束 turn）。

    编排两步（顺序与方案 §六一致，不可调换）：
    1. 还原：从 file_snapshots 读出反向操作按 seq 降序 apply，将磁盘还原到执行前。
       还原失败（apply_all_with_diff 抛错）直接向上抛出，turn 状态保持原样，可重入续跑。
    2. 清空：删除 checkpoint thread、清空 turn_messages、清空 file_snapshots、
       更新 turn.status=reverted、写 runtime_event(turn_reverted)。此步幂等。

    参数:
        turn_id: 待回退的轮次标识。
        workspace_root: 调用方注入的 workspace 根路径；为 None 时经
            task→workspace 关系解析（兜底路径）。

    返回:
        RevertResult：ok 表示整体成功；non_revertible_actions 携带无法靠快照还原
        的动作（如 execute_terminal）提示。

    异常:
        ValueError: 当 turn 仍在运行（status in running/cancelling）或不是该 task
            最新已结束 turn（D6）时抛出，对应 API 层映射为 409。
        RuntimeError: 当文件还原失败（apply_all_with_diff 抛错）时向上抛出，供
            调用方记录并支持重入。

    副作用:
        修改文件系统、删除 checkpoint thread、清空消息与快照、改写 turn 状态、写审计事件。
    """
    turn_crud = TurnCrud()
    turn = turn_crud.get(turn_id)

    # 守卫 1：仅已结束 turn 可回退（快速失败，非最新校验留待持锁后 double-check）
    if turn.status not in _FINISHED_STATUSES:
        raise ValueError(f"turn not finished; cancel first (status={turn.status})")

    lock = await _acquire_lock(turn_id)
    async with lock:
        # 守卫 2（持锁后复核，消除 TOCTOU 竞态）：D6 仅最新已结束 turn 可回退。
        # reverted 视为已结束且允许重入幂等，故跳过本校验。
        if turn.status != TurnStatus.REVERTED.value and not _is_latest_finished_turn(
            turn_crud, turn_id
        ):
            raise ValueError("only the latest finished turn can be reverted")
        return await _revert_turn_locked(turn_id, turn.task_id, workspace_root)


async def _revert_turn_locked(
    turn_id: str, task_id: str, workspace_root: Path | None = None
) -> RevertResult:
    """在已持锁前提下执行回退（清空 + 还原）。

    参数:
        turn_id: 待回退轮次。
        task_id: 所属任务（用于审计事件与 workspace 解析）。

    返回:
        RevertResult。

    异常:
        RuntimeError: 文件还原失败时向上抛出（此时尚未清空状态，turn 保持原样可重入）。

    副作用:
        见 :func:`revert_turn`。
    """
    snapshot_crud = FileSnapshotCrud()
    snapshots = snapshot_crud.list_by_turn(turn_id)
    non_revertible = _collect_non_revertible(turn_id)

    # 步骤 1：先还原文件（方案 §六 顺序：还原成功后再清状态，避免半回退态）。
    # 还原失败（apply_all_with_diff 抛错）直接向上抛出，turn 状态保持原样，可重入续跑。
    file_reverted = False
    if snapshots:
        file_reverted = await _restore_files(task_id, snapshots, workspace_root)

    # 步骤 2：清空对话轨迹与状态（幂等：重复调用结果一致）
    await _clear_turn_state(turn_id, task_id)

    log.info(
        "turn_reverted",
        extra={
            "msg": "turn 已回退：文件与对话轨迹已还原",
            "data": {
                "turn_id": turn_id,
                "task_id": task_id,
                "snapshot_count": len(snapshots),
                "file_reverted": file_reverted,
                "non_revertible_actions": non_revertible,
            },
        },
    )
    return RevertResult(
        ok=True,
        turn_id=turn_id,
        file_reverted=file_reverted,
        non_revertible_actions=non_revertible,
    )


# 不由文件快照承载、回退时无法还原磁盘副作用的动作（方案 D5：execute_terminal）。
_NON_REVERTIBLE_TOOLS = frozenset({"execute_terminal"})


def _collect_non_revertible(turn_id: str) -> list[str]:
    """汇总该 turn 中不可靠文件快照还原的动作名（D5：execute_terminal 等）。

    文件类工具（write_file / patch / delete）的副作用已由 ``file_snapshots`` 承载，
    可完整回退；命令执行类工具（execute_terminal）等越出文件范畴、不经文件快照，
    其副作用无法靠反向操作还原，需在此单独从运行时事件识别并提示前端。

    参数:
        turn_id: 待回退轮次（用于查询其运行时事件）。

    返回:
        不可回退动作名去重列表；若该 turn 仅含可回退的文件操作，返回空列表。
    """
    events = RuntimeEventCrud().list_by_turn(turn_id)
    actions: list[str] = []
    for event in events:
        if event.get("event_type") != "tool_call_started":
            continue
        tool_name = (event.get("payload") or {}).get("tool_name", "")
        if tool_name in _NON_REVERTIBLE_TOOLS and tool_name not in actions:
            actions.append(tool_name)
    return actions


async def _clear_turn_state(turn_id: str, task_id: str) -> None:
    """清空 turn 的对话轨迹与状态（两段式的第 2 步，幂等）。

    删除 checkpoint thread（thread_id == turn_id）、清空 turn_messages、清空
    file_snapshots、更新 status=reverted、写审计事件。

    参数:
        turn_id: 待清空轮次。
        task_id: 所属任务（审计事件与 checkpoint 关联）。

    返回:
        无。

    异常:
        SQLAlchemyError / OSError: 底层存储失败时向上抛出（可重入）。

    副作用:
        删除 checkpoint thread、清空两张表、改写状态、写一条 runtime_event。
    """
    # 业务库 4 步按依赖顺序执行，保证中途失败可重入：
    # 先删消息与清空快照（数据清理），再把 status 标记为 REVERTED（屏障），最后写审计事件。
    # 若 update_status 成功后仍有后续失败，重入时 status=REVERTED 仍可被 guard 放行，
    # 此时快照若还在可再次还原；若快照已清则 restore 无操作，整体幂等。
    TurnMessageCrud().delete_by_turn_ids([turn_id])
    FileSnapshotCrud().clear_by_turn(turn_id)
    TurnCrud().update_status(turn_id, TurnStatus.REVERTED.value, end_reason="reverted")
    RuntimeEventCrud().save_event(
        {
            "event_id": new_event_id(),
            "event_type": "turn_reverted",
            "task_id": task_id,
            "turn_id": turn_id,
            "sequence": RuntimeEventCrud().next_sequence_for_turn(turn_id),
            "payload": {"turn_id": turn_id},
            "created_at": "",
        }
    )
    # checkpoint thread 在独立库，作为业务库提交后的补偿步骤：即使此处失败，
    # 业务态已一致（status=REVERTED），可后续单独重试删除，不影响文件还原与消息清理。
    # 因此失败仅记 warning 不向上抛，避免已成功的回退被误判为失败而触发重复调用。
    try:
        async with AsyncSqliteSaver.from_conn_string(checkpoint_path()) as saver:
            await saver.setup()
            await saver.adelete_thread(turn_id)
    except Exception:
        log.warning(
            "turn_revert_checkpoint_delete_failed",
            extra={
                "msg": "回退后删除 checkpoint thread 失败，业务态已一致，可后续单独重试",
                "data": {"turn_id": turn_id, "task_id": task_id},
            },
        )


async def _restore_files(
    task_id: str,
    snapshots: list[FileSnapshotRecord],
    workspace_root: Path | None = None,
) -> bool:
    """按 seq 降序应用反向操作，将磁盘还原到 turn 执行前（两段式的第 1 步）。

    参数:
        task_id: 所属任务，用于解析 workspace 根路径（兜底）。
        snapshots: 已按 seq 降序排列的快照记录（list_by_turn 已保证）。
        workspace_root: 调用方注入的 workspace 根；为 None 时经 task→workspace 解析。

    返回:
        True 表示成功应用了至少一个反向操作。

    异常:
        RuntimeError: apply_all_with_diff 失败时向上抛出（调用方记录后可重入）。

    副作用:
        修改 workspace 内文件（增/删/改/移），复用 ``apply_all_with_diff`` 路径
        containment 校验，越界路径会被拒绝。
    """
    operations = _snapshots_to_operations(snapshots)
    if not operations:
        return False
    if workspace_root is None:
        workspace_root = _resolve_workspace_root(task_id)
    resolver = ProjectPathResolver(workspace_root)
    # 按 seq 降序逐个应用，每个操作应用前基于「当前磁盘态」判断是否已在上一次
    # （被中断的）回退中完成；已完成则跳过。顺序判断（而非批量预检）保证了
    # 正常回退时后面的 DELETE 不会因前面的 ADD 尚未执行而被误判为「已删」，
    # 也保证了部分失败重入时已完成的前置操作被正确跳过（方案 §六「可重入」落地）。
    for op in operations:
        if _is_already_reverted(op, resolver):
            continue
        try:
            apply_all_with_diff([op], resolver)
        except Exception:
            # 文件还原是高危破坏性操作的关键失败路径，必须记录定位信息（哪个 op、
            # 哪个文件、操作类型），便于线上排查；异常向上抛由 API 层映射为失败响应，
            # turn 状态保持原样可重入续跑。
            log.exception(
                "turn_revert_restore_failed",
                extra={
                    "msg": "turn 回退文件还原失败",
                    "data": {
                        "turn_id": snapshots[0].turn_id,
                        "operation": op.operation.value,
                        "file_path": op.file_path,
                    },
                },
            )
            raise
    return True


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
            # 用 utf-8-sig 读取以剥离可能的 BOM 前缀，与 expected（content 不含 BOM
            # 前缀）可比；否则含 BOM 文件重入时会因 read_text 带 \ufeff 而判定未还原，
            # 导致重复 apply 触发 destination-already-exists 卡死。
            current = Path(resolved).read_bytes().decode("utf-8-sig")
            return current == expected
        except OSError:
            return False
    elif operation.operation == OperationType.DELETE:
        return not Path(resolved).exists()
    elif operation.operation == OperationType.UPDATE:
        if operation.content is None or not Path(resolved).exists():
            return False
        try:
            current = Path(resolved).read_bytes().decode("utf-8-sig")
            return current == operation.content
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
    """从操作的 hunks 拼接 '+' 行内容（content 字段缺失时的降级还原文本）。"""
    return "\n".join(
        line.content for hunk in operation.hunks for line in hunk.lines if line.prefix == "+"
    )


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
