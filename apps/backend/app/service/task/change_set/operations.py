"""变更集单文件操作编排（保留 / 撤销）。

单一职责：把「把某文件的最新变更标记为保留」与「撤销某文件的最新变更」两个
操作编排起来——读取最新快照、CAS 更新处理态、必要时经 ``apply_all_with_diff``
应用反向操作、撤销成功后广播实时事件。不写 SQL、不承载查询聚合。

设计边界：
- 查询辅助（``_require_latest_any``）来自 ``query`` 模块，冲突判定来自
  ``conflict`` 模块，反序列化来自 ``snapshot_patch`` 模块，本文件只做编排。
- 快照处理态写入一律走 CAS（``_cas_update_status``），防并发 lost update。
"""

import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from app.config.logging.logger import log
from app.core.tools.tool_handler.patch_write.patch_apply import PatchApplyError, apply_all_with_diff
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.models.result.change_set import ChangeFileEntry
from app.service.depends import get_task_service, get_workspace_service
from app.service.task.change_set.conflict import _is_already_reverted, _is_at_after_state
from app.service.task.change_set.errors import ChangeSetConflictError
from app.service.task.change_set.query import _require_latest_any
from app.service.task.change_set.snapshot_patch import snapshots_to_operations
from app.storage.crud.file_snapshot_crud import FileSnapshotCrud

# 进程内 (task_id, path) 互斥锁表：串行化同 path 的 keep/revert，消除「revert 先 apply
# 磁盘、后 CAS 状态」窗口被并发 keep 插入导致「磁盘已还原而 DB 标 kept」的语义破坏。
# 锁对象是「task 快照行」而非物理文件（keep 无 workspace 依赖，无法解析绝对路径），
# 与 tools 层 FilePathLockRegistry（物理文件写入锁）语义不同，故不复用。
# 条目只增不减（每个 (task_id, path) 一个 threading.Lock，量级为操作过的路径数，
# 内存可忽略；task 删除后残留条目无害，仅作常驻引用）。
_PATH_LOCKS: dict[tuple[int, str], threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(task_id: int, path: str) -> threading.Lock:
    """取得 (task_id, path) 的进程内互斥锁（惰性创建、常驻）。

    参数:
        task_id: 任务标识。
        path: 相对 workspace 的文件路径。

    返回:
        与 (task_id, path) 一一对应的 ``threading.Lock``。

    异常:
        无。

    副作用:
        首次访问时向 ``_PATH_LOCKS`` 写入一个新锁条目。
    """
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault((task_id, path), threading.Lock())


def keep_file(task_id: int, path: str) -> ChangeFileEntry:
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
        持 ``(task_id, path)`` 进程内锁期间改写 ``file_snapshots`` 中一行的 ``status``；
        写一条 info 日志。不触碰磁盘。
    """
    with _path_lock(task_id, path):
        snapshot = _require_latest_any(task_id, path)
        _cas_update_status(snapshot.id, "kept", task_id, path, snapshot.run_id)
    log.info(
        "change_set_file_kept",
        extra={
            "msg": "变更集：文件变更已标记为保留",
            "data": {"task_id": task_id, "path": path, "run_id": snapshot.run_id},
        },
    )
    return ChangeFileEntry.from_snapshot(snapshot, "kept")


async def revert_file(
    task_id: int, path: str, workspace_root: Path | None = None
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
        ChangeSetConflictError: 当该快照 ``status`` 已被并发改态（如被 ``keep_file``
            保留）时抛出（API 映射 409）。该校验发生在 ``(task_id, path)`` 进程内锁内、
            apply 磁盘之前：已非 ``pending``/``reverted`` 时直接拒绝且**不改动磁盘**，
            避免「用户已保留的改动被整文件覆盖还原」的静默破坏（CAS miss 作为锁外的
            最后一道 lost update 防线保留）。

    副作用:
        持 ``(task_id, path)`` 进程内锁期间修改 workspace 内文件；改写
        ``file_snapshots`` 的 ``status`` 与 ``reverted_at``；经事件总线广播
        ``FILE_CHANGE_UPDATED``；写日志。
    """
    if workspace_root is None:
        workspace_root = _resolve_workspace_root(task_id)
    with _path_lock(task_id, path):
        snapshot = _require_latest_any(task_id, path)
        if snapshot.status not in ("pending", "reverted"):
            # 已被并发 keep（或其它状态）改态：拒绝撤销且不得触碰磁盘。
            log.warning(
                "change_set_revert_status_mutated",
                extra={
                    "msg": "变更集：快照处理态已非可撤销状态，撤销被拒（磁盘未改动）",
                    "data": {
                        "task_id": task_id,
                        "path": path,
                        "run_id": snapshot.run_id,
                        "status": snapshot.status,
                    },
                },
            )
            raise ChangeSetConflictError(f"change status already mutated, revert rejected: {path}")
        resolver = PathResolver(workspace_root)
        for operation in snapshots_to_operations([snapshot]):
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
                            "run_id": snapshot.run_id,
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
                            "run_id": snapshot.run_id,
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
            snapshot.run_id,
            reverted_at=datetime.now(UTC).isoformat(),
            expected_statuses=("pending", "reverted"),
        )
    log.info(
        "change_set_file_reverted",
        extra={
            "msg": "变更集：文件变更已撤销",
            "data": {"task_id": task_id, "path": path, "run_id": snapshot.run_id},
        },
    )
    return ChangeFileEntry.from_snapshot(snapshot, "reverted")


def _cas_update_status(
    snapshot_id: int,
    status: str,
    task_id: int,
    path: str,
    run_id: int,
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
        run_id: 所属轮次标识（仅用于日志）。
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
                "data": {"task_id": task_id, "path": path, "run_id": run_id, "target": status},
            },
        )
        raise ChangeSetConflictError(f"change status already mutated, operation rejected: {path}")


def _resolve_workspace_root(task_id: int) -> Path:
    """解析任务所属 workspace 的根路径（兜底路径）。

    与 ``conversation_run_workspace_resolver`` 同构的「task → workspace → root_path」解析链，但
    输入是 ``task_id``（而非 ``ConversationRunRecord``）、失败抛 ``KeyError``（而非静默返回 None），
    故不复用 ``ConversationRunWorkspaceResolver``，只在此统一依赖获取——所有依赖经
    ``service_depends`` 进程单例取得，不做裸 ``new`` 构造。

    参数:
        task_id: 任务标识。

    返回:
        workspace 根目录的 :class:`Path`。

    异常:
        KeyError: 当 task 或 workspace 不存在时抛出。

    副作用:
        无（只读）。
    """
    task = get_task_service().get_task(task_id)
    workspace = get_workspace_service().get_workspace(task.workspace_id)
    return Path(workspace.root_path)
