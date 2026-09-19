"""Task-scoped net ChangeSet projection, Keep, and baseline Revert use cases."""

from __future__ import annotations

import errno
import hashlib
import os
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from app.config.logging.logger import log
from app.core.tools.guard.file_state import get_shared_file_path_lock_registry
from app.core.tools.guard.trash_staging import parse_trash_ref, purge_trash, resolve_trash_path
from app.core.tools.tool_handler.patch_write.patch_diff import (
    FileDiffResult,
    build_diff_stats,
    format_git_diff,
)
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.models.file_snapshot_record import FileSnapshotRecord
from app.models.task_record import TaskRecord
from app.service.task.change_set.blob_garbage_collector import ChangeSetBlobGarbageCollector
from app.service.task.change_set.blob_store import ChangeSetBlobStore
from app.service.task.change_set.file_state import (
    CapturedPath,
    WorkspacePathChanged,
    capture_path,
    directory_identity,
    restore_path_state,
    state_matches,
)
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.file_snapshot_crud import FileSnapshotCrud
from app.storage.store_engines import main_session_factory
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

_PATCH_LIMIT = 16_000


@dataclass(frozen=True)
class _ChangeGroup:
    """A contiguous set of snapshot rows bounded by a Keep/Revert decision."""

    change_id: str
    key: str
    status: str
    snapshots: tuple[FileSnapshotRecord, ...]
    paths: tuple[str, ...]


@dataclass(frozen=True)
class _ValidatedGroup:
    """Verified baseline/current states for every path in one ChangeSet group."""

    baseline: dict[str, dict[str, Any]]
    final: dict[str, dict[str, Any]]
    current: dict[str, CapturedPath]
    identities: dict[str, tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]]


class _RevertExternalEdit(RuntimeError):
    """A path changed after Revert preflight, before its restore step."""


class TaskChangeSetService:
    """Project final net diffs and mutate complete pending groups to a baseline.

    SQLite owns snapshot facts. Workspace files remain the live state. All
    file reads/writes are protected by the same process-wide physical path locks as
    structured Agent tools; Keep/Revert additionally hold the Task operation gate and
    check canonical Run rows before changing snapshot or file state.
    """

    def __init__(
        self,
        *,
        snapshot_crud: FileSnapshotCrud | None = None,
        run_crud: ConversationRunCrud | None = None,
        blob_store: ChangeSetBlobStore | None = None,
    ) -> None:
        self._snapshots = snapshot_crud or FileSnapshotCrud()
        self._runs = run_crud or ConversationRunCrud()
        self._blobs = blob_store or ChangeSetBlobStore()
        self._blob_gc = ChangeSetBlobGarbageCollector(self._blobs, self._snapshots)

    def get_changes(self, task_id: int) -> dict[str, Any]:
        """Return pending task groups with a verified final diff from their baseline."""

        task, root = self._task_and_root(task_id)
        while True:
            initial_groups = [group for group in self._groups(task_id) if group.status == "pending"]
            initial_paths = sorted({path for group in initial_groups for path in group.paths})
            scope = self._lock_scope(root, initial_paths)
            with self._path_locks(root, initial_paths):
                groups = [group for group in self._groups(task_id) if group.status == "pending"]
                paths = {path for group in groups for path in group.paths}
                if not all(self._scope_covers(root, scope, path) for path in paths):
                    continue
                files = [self._project_group(group, root) for group in groups]
                return {
                    "task_id": task_id,
                    "files": files,
                }

    def apply(self, task_id: int, action: str, change_ids: Sequence[str]) -> dict[str, Any]:
        """Apply Keep or whole-group baseline Revert independently per requested group."""

        if action not in {"keep", "revert"}:
            raise ValueError(f"unsupported ChangeSet action: {action}")
        task, root = self._task_and_root(task_id)
        unique_ids = list(dict.fromkeys(item for item in change_ids if item))
        results: list[dict[str, Any]] = []
        try:
            task_space = task_runtime_spaces.get_or_create(task_id)
            with task_space.operation(timeout=0):
                if self._runs.has_active_for_task(task_id):
                    results = [
                        self._result(item, "failed", "operation_busy") for item in unique_ids
                    ]
                else:
                    groups_by_id = {group.change_id: group for group in self._groups(task_id)}
                    if action == "revert":
                        unique_ids.sort(
                            key=lambda item: groups_by_id[item].snapshots[-1].seq
                            if item in groups_by_id and groups_by_id[item].status == "pending"
                            else -1,
                            reverse=True,
                        )
                    for change_id in unique_ids:
                        results.append(self._apply_one(task_id, root, action, change_id))
        except TimeoutError:
            results = [self._result(item, "failed", "operation_busy") for item in unique_ids]
        return {
            "task_id": task_id,
            "results": results,
            "change_set": self.get_changes(task_id),
        }

    def _apply_one(
        self,
        task_id: int,
        root: Path,
        action: str,
        change_id: str,
    ) -> dict[str, Any]:
        group = next((item for item in self._groups(task_id) if item.change_id == change_id), None)
        if group is None:
            return self._result(change_id, "stale_change_id")
        if group.status != "pending":
            outcome = "already_kept" if group.status == "kept" else "already_reverted"
            return self._result(change_id, outcome)

        try:
            with self._path_locks(root, group.paths):
                refreshed = next(
                    (item for item in self._groups(task_id) if item.change_id == change_id), None
                )
                if refreshed is None or refreshed.status != "pending":
                    if refreshed is not None:
                        done = "already_kept" if refreshed.status == "kept" else "already_reverted"
                        return self._result(change_id, done)
                    return self._result(change_id, "stale_change_id")
                if action == "keep":
                    return self._keep(refreshed)
                return self._revert(task_id, root, refreshed)
        except Exception:
            log.exception(
                "task_change_set_action_failed",
                extra={
                    "msg": "Task ChangeSet 文件组操作失败",
                    "data": {"task_id": task_id, "change_id": change_id, "action": action},
                },
            )
            return self._result(change_id, "failed")

    def _keep(self, group: _ChangeGroup) -> dict[str, Any]:
        """把一组 pending 变更接受为当前工作区的新基线（确认删除则真正清除 trash 副本）。

        参数:
            group: 待接受的一组共享同一变更标识的 pending 快照。

        返回:
            ChangeSet 应用结果（``outcome="kept"``）。

        异常:
            RuntimeError: 快照在事务提交前状态已变化（乐观并发失败）。

        副作用:
            在事务内把快照置为 ``kept``、弹出回退引用并触发 blob 垃圾回收；对删除类操作额外 purge
            其 trash 暂存目录（最终删除）；记结构化日志。
        """

        with main_session_factory().begin() as session:
            count = self._snapshots.update_statuses(
                [snapshot.id for snapshot in group.snapshots],
                "kept",
                expected_status="pending",
                session=session,
            )
            if count != len(group.snapshots):
                raise RuntimeError("ChangeSet changed while applying Keep")
        # keep 表示接受当前（已删除）状态为新基线：把 trash 暂存副本真正清除。
        self._purge_group_trash(group)
        self._collect_garbage_best_effort()
        log.info(
            "task_change_set_kept",
            extra={
                "msg": "Task ChangeSet 当前状态已设为新基线",
                "data": {"task_id": group.snapshots[0].task_id, "change_id": group.change_id},
            },
        )
        return self._result(group.change_id, "kept")

    def _purge_group_trash(self, group: _ChangeGroup) -> None:
        """清理一组 ChangeSet 记录所对应操作的 trash 暂存目录（确认删除后调用）。"""
        try:
            _, root = self._task_and_root(group.snapshots[0].task_id)
        except (OSError, ValueError, RuntimeError):
            return
        operation_ids: set[str] = set()
        for snapshot in group.snapshots:
            envelope = cast(dict[str, Any], snapshot.op_json)
            operation = envelope.get("operation")
            operation_id = operation.get("operation_id") if isinstance(operation, dict) else None
            if isinstance(operation_id, str) and operation_id.startswith("mut_"):
                operation_ids.add(operation_id)
        for operation_id in operation_ids:
            purge_trash(root, operation_id)

    def _revert(
        self,
        task_id: int,
        root: Path,
        group: _ChangeGroup,
    ) -> dict[str, Any]:
        validation, failure = self._validate_group(group, root)
        if failure:
            return self._result(group.change_id, failure)
        assert validation is not None
        side_effect_directory_ids = _group_side_effect_directory_ids(group)
        restore_targets = {
            path: state
            for path, state in validation.baseline.items()
            if not state_matches(validation.current[path].state, state)
        }
        if not restore_targets:
            self._mark_reverted(group)
            return self._result(group.change_id, "reverted")
        mutation_id = f"rev_{uuid.uuid4().hex}"
        with main_session_factory().begin() as session:
            marked = self._snapshots.begin_revert(
                [snapshot.id for snapshot in group.snapshots],
                mutation_id,
                session=session,
            )
            if marked != len(group.snapshots):
                raise RuntimeError("ChangeSet changed while preparing Revert")

        try:
            current = self._capture_states(root, restore_targets)
        except (OSError, ValueError, RuntimeError):
            current = {}
        if any(
            path not in current or not state_matches(current[path], validation.current[path].state)
            for path in restore_targets
        ) or any(
            directory_identity(root / Path(path)) != identity
            for path, identity in side_effect_directory_ids.items()
        ):
            self._reconcile_reverting_group(mutation_id, group.snapshots, root)
            return self._result(group.change_id, "conflict")

        try:
            self._restore_states(
                root,
                restore_targets,
                expected_current={path: validation.current[path].state for path in restore_targets},
                defer_nonempty_absent_paths=set(side_effect_directory_ids),
                expected_directory_ids=side_effect_directory_ids,
            )
            restored = self._capture_states(root, restore_targets)
            for path in restore_targets:
                if state_matches(restored[path], restore_targets[path]):
                    continue
                if (
                    path in side_effect_directory_ids
                    and not restore_targets[path].get("exists")
                    and restored[path].get("exists")
                    and restored[path].get("entry_type") == "directory"
                    and directory_identity(root / Path(path)) == side_effect_directory_ids[path]
                ):
                    # A sibling change still uses this Agent-created directory. Keep
                    # it for now; another file group can remove it after its last child.
                    continue
                raise RuntimeError("workspace did not reach the ChangeSet baseline")
            with main_session_factory().begin() as session:
                count = self._snapshots.update_statuses(
                    [snapshot.id for snapshot in group.snapshots],
                    "reverted",
                    expected_status="pending",
                    session=session,
                )
                if count != len(group.snapshots):
                    raise RuntimeError("ChangeSet changed while applying Revert")
        except _RevertExternalEdit:
            self._reconcile_reverting_group(mutation_id, group.snapshots, root)
            return self._result(group.change_id, "conflict")
        except Exception:
            self._reconcile_reverting_group(mutation_id, group.snapshots, root)
            log.exception(
                "task_change_set_revert_interrupted",
                extra={
                    "msg": "ChangeSet 回退中断；按当前文件状态更新变更集",
                    "data": {"task_id": task_id, "change_id": group.change_id},
                },
            )
            return self._result(group.change_id, "failed")

        log.info(
            "task_change_set_reverted_to_baseline",
            extra={
                "msg": "Task ChangeSet 整组文件已回退到最近基线",
                "data": {"task_id": task_id, "change_id": group.change_id},
            },
        )
        self._collect_garbage_best_effort()
        return self._result(group.change_id, "reverted")

    def reconcile_interrupted_reverts(self) -> list[str]:
        """Reconcile interrupted ChangeSet reverts from the current workspace state."""

        groups: dict[str, list[FileSnapshotRecord]] = defaultdict(list)
        for record in self._snapshots.list_unfinalized():
            if record.mutation_state == "reverting":
                groups[record.mutation_id].append(record)
        unresolved: set[str] = set()
        for mutation_id, records in groups.items():
            try:
                task_id = records[0].task_id
                _, root = self._task_and_root(task_id)
                self._reconcile_reverting_group(mutation_id, records, root)
            except Exception:
                log.exception(
                    "change_set_revert_reconcile_failed",
                    extra={
                        "msg": "中断的 ChangeSet 回退暂时无法对账，保留快照待下次启动重试",
                        "data": {"mutation_id": mutation_id},
                    },
                )
                for record in records:
                    unresolved.add(record.path)
        self._collect_garbage_best_effort()
        return sorted(unresolved)

    def _reconcile_reverting_group(
        self,
        mutation_id: str,
        records: Sequence[FileSnapshotRecord],
        root: Path,
    ) -> None:
        """Update Revert rows to match current disk state without restoring any file."""

        actual: dict[str, dict[str, Any]] = {}
        states_by_id: dict[int, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        identities_by_id: dict[int, set[str]] = {}
        baseline_by_identity: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        last_state_by_identity: dict[str, dict[str, tuple[int, int]]] = defaultdict(dict)
        ordered_records = sorted(records, key=lambda record: record.seq)
        for record in ordered_records:
            envelope: dict[str, Any] = dict(record.op_json)
            path_states = envelope.get("path_states")
            if not isinstance(path_states, list) or not path_states:
                raise ValueError("reverting snapshot path states are malformed")
            states_by_id[record.id] = (envelope, path_states)
            row_identities: set[str] = set()
            for index, entry in enumerate(path_states):
                if not isinstance(entry, dict):
                    raise ValueError("reverting snapshot path state is malformed")
                path = entry.get("path")
                identity = entry.get("file_identity")
                before = entry.get("before")
                if not isinstance(path, str) or not isinstance(identity, str) or not identity:
                    raise ValueError("reverting snapshot path identity is malformed")
                if not isinstance(before, dict):
                    raise ValueError("reverting snapshot before state is malformed")
                row_identities.add(identity)
                baseline_by_identity[identity].setdefault(path, before)
                last_state_by_identity[identity][path] = (record.id, index)
                actual.setdefault(path, {})
            identities_by_id[record.id] = row_identities
            side_effect_states = envelope.get("side_effect_states", [])
            if not isinstance(side_effect_states, list):
                raise ValueError("reverting snapshot side-effect states are malformed")
            for entry in side_effect_states:
                path = entry.get("path") if isinstance(entry, dict) else None
                if isinstance(path, str):
                    actual.setdefault(path, {})
        actual = self._capture_states(root, actual)
        at_baseline_by_identity = {
            identity: all(
                state_matches(actual[path], baseline) for path, baseline in path_states.items()
            )
            for identity, path_states in baseline_by_identity.items()
        }
        will_revert_by_id = {
            record_id: all(at_baseline_by_identity[identity] for identity in identities)
            for record_id, identities in identities_by_id.items()
        }
        for path_states in last_state_by_identity.values():
            for path, (record_id, index) in path_states.items():
                if not will_revert_by_id[record_id]:
                    states_by_id[record_id][1][index]["after"] = actual[path]

        updates: list[dict[str, object]] = []
        for record in ordered_records:
            envelope, path_states = states_by_id[record.id]
            reverted = will_revert_by_id[record.id]
            side_effect_states = envelope.get("side_effect_states", [])
            if reverted:
                for entry in path_states:
                    entry.get("before", {}).pop("restore_ref", None)
            else:
                for entry in side_effect_states:
                    path = entry.get("path")
                    if isinstance(path, str) and path in actual:
                        entry["after"] = actual[path]
            updates.append(
                {
                    "id": record.id,
                    "op_json": envelope,
                    "status": "reverted" if reverted else "pending",
                }
            )
        updated = self._snapshots.update_mutation_records(mutation_id, updates)
        if updated != len(records):
            raise RuntimeError("ChangeSet revert marker changed during reconciliation")

    def _mark_reverted(self, group: _ChangeGroup) -> None:
        with main_session_factory().begin() as session:
            count = self._snapshots.update_statuses(
                [snapshot.id for snapshot in group.snapshots],
                "reverted",
                expected_status="pending",
                session=session,
            )
            if count != len(group.snapshots):
                raise RuntimeError("ChangeSet changed while marking a no-op Revert")
        self._collect_garbage_best_effort()

    def _collect_garbage_best_effort(self) -> None:
        try:
            self._blob_gc.collect()
        except Exception:
            log.exception(
                "change_set_blob_gc_failed",
                extra={
                    "msg": "ChangeSet 恢复对象清理失败；后续启动或收口操作会重试",
                    "data": {},
                },
            )

    def _project_group(self, group: _ChangeGroup, root: Path) -> dict[str, Any]:
        validation, failure = self._validate_group(group, root)
        if failure:
            status = "pending"
            net_diff = _empty_net_diff("conflict" if failure == "conflict" else "unverifiable")
            action = self._action(group, None)
        else:
            status = "pending"
            try:
                net_diff = self._net_diff(validation)
            except (OSError, ValueError, RuntimeError):
                net_diff = _empty_net_diff("unverifiable")
            action = self._action(group, validation)
        operation_ids: set[tuple[str, str | int]] = set()
        for snapshot in group.snapshots:
            envelope = cast(dict[str, Any], snapshot.op_json)
            operation = envelope.get("operation", {})
            operation_id = operation.get("operation_id") if isinstance(operation, dict) else None
            if isinstance(operation_id, str) and operation_id:
                operation_ids.add(("operation", operation_id))
            else:
                operation_ids.add(("snapshot", snapshot.id))

        return {
            "change_id": group.change_id,
            "paths": list(group.paths),
            "action": action,
            "status": status,
            "last_run_id": group.snapshots[-1].run_id if group.snapshots else None,
            "operation_count": len(operation_ids),
            "net_diff": net_diff,
        }

    def _validate_group(
        self,
        group: _ChangeGroup,
        root: Path,
    ) -> tuple[_ValidatedGroup | None, str | None]:
        baseline: dict[str, dict[str, Any]] = {}
        final: dict[str, dict[str, Any]] = {}
        identity_baseline: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        identity_final: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        side_effect_directory_ids: dict[str, str] = {}
        for snapshot in group.snapshots:
            envelope = cast(dict[str, Any], snapshot.op_json)
            states = envelope.get("path_states")
            if not isinstance(states, list) or not states:
                return None, "snapshot_unverifiable"
            side_effect_states = envelope.get("side_effect_states", [])
            if not isinstance(side_effect_states, list):
                return None, "snapshot_unverifiable"
            for side_effect in side_effect_states:
                if not isinstance(side_effect, dict):
                    return None, "snapshot_unverifiable"
                path = side_effect.get("path")
                before = side_effect.get("before")
                after = side_effect.get("after")
                directory_id = side_effect.get("directory_id")
                if (
                    not isinstance(path, str)
                    or not path
                    or "\\" in path
                    or Path(path).is_absolute()
                    or ".." in Path(path).parts
                    or not isinstance(before, dict)
                    or before.get("exists") is not False
                    or not isinstance(after, dict)
                    or after.get("exists") is not True
                    or after.get("entry_type") != "directory"
                    or not isinstance(directory_id, str)
                    or not directory_id
                ):
                    return None, "snapshot_unverifiable"
                if path in final and not state_matches(final[path], before):
                    return None, "conflict"
                if path not in baseline:
                    baseline[path] = before
                final[path] = after
                side_effect_directory_ids[path] = directory_id
            for entry in states:
                if not isinstance(entry, dict):
                    return None, "snapshot_unverifiable"
                path = entry.get("path")
                identity = entry.get("file_identity")
                before = entry.get("before")
                after = entry.get("after")
                if (
                    not isinstance(path, str)
                    or not path
                    or "\\" in path
                    or Path(path).is_absolute()
                    or not isinstance(identity, str)
                    or not isinstance(before, dict)
                    or not isinstance(after, dict)
                    or not isinstance(before.get("exists"), bool)
                    or not isinstance(after.get("exists"), bool)
                ):
                    return None, "snapshot_unverifiable"
                if before.get("exists") and before.get("entry_type") in {"file", "symlink"}:
                    restore_ref = before.get("restore_ref")
                    if not isinstance(restore_ref, str):
                        return None, "snapshot_unverifiable"
                    if parse_trash_ref(restore_ref) is not None:
                        # 删除类回退事实在 trash 暂存区：校验副本仍存在。
                        trash_path = resolve_trash_path(root, restore_ref)
                        if trash_path is None or not trash_path.exists():
                            return None, "snapshot_unverifiable"
                    else:
                        try:
                            self._blobs.read(restore_ref)
                        except (OSError, ValueError, RuntimeError):
                            return None, "snapshot_unverifiable"
                if path in final and not state_matches(final[path], before):
                    return None, "conflict"
                if path not in baseline:
                    baseline[path] = before
                    identity_baseline[identity][path] = before
                elif path not in identity_baseline[identity]:
                    identity_baseline[identity][path] = before
                final[path] = after
                identity_final[identity][path] = after

        try:
            current = {path: capture_path(root, root / Path(path)) for path in baseline}
        except (OSError, ValueError, RuntimeError):
            return None, "snapshot_unverifiable"
        if any(not state_matches(current[path].state, final[path]) for path in final):
            return None, "conflict"
        if any(
            directory_identity(root / Path(path)) != expected_identity
            for path, expected_identity in side_effect_directory_ids.items()
        ):
            return None, "conflict"

        identities = {
            identity: (identity_baseline[identity], identity_final[identity])
            for identity in identity_baseline.keys() | identity_final.keys()
        }
        return _ValidatedGroup(baseline, final, current, identities), None

    def _net_diff(self, validation: _ValidatedGroup | None) -> dict[str, Any]:
        if validation is None:
            return _empty_net_diff("unverifiable")
        diff_results: list[FileDiffResult] = []
        changed_paths = {
            path
            for path in validation.baseline.keys() | validation.final.keys()
            if not state_matches(
                validation.baseline.get(path, {"exists": False}),
                validation.final.get(path, {"exists": False}),
            )
        }
        rendered_paths: set[str] = set()
        for base_paths, final_paths in validation.identities.values():
            base_files = {
                path: state
                for path, state in base_paths.items()
                if state.get("exists") and state.get("entry_type") == "file"
            }
            final_files = {
                path: state
                for path, state in final_paths.items()
                if state.get("exists") and state.get("entry_type") == "file"
            }
            if len(base_files) == 1 and len(final_files) == 1:
                old_path, old_state = next(iter(base_files.items()))
                new_path, new_state = next(iter(final_files.items()))
                if old_path == new_path and state_matches(old_state, new_state):
                    continue
                before_text = self._baseline_text(old_state)
                after_capture = validation.current[new_path]
                after_text = _captured_text(after_capture)
                if before_text is not None and after_text is not None:
                    status = "added" if not old_state.get("exists") else "modified"
                    diff_results.append(
                        FileDiffResult(
                            path=old_path,
                            status=status,
                            before=before_text,
                            after=after_text,
                            new_path=new_path if old_path != new_path else None,
                        )
                    )
                    if before_text != after_text:
                        rendered_paths.update((old_path, new_path))
                continue

            paired = set(base_files) & set(final_files)
            for path in paired:
                if state_matches(base_files[path], final_files[path]):
                    continue
                before_text = self._baseline_text(base_files[path])
                after_text = _captured_text(validation.current[path])
                if before_text is not None and after_text is not None:
                    diff_results.append(FileDiffResult(path, "modified", before_text, after_text))
                    if before_text != after_text:
                        rendered_paths.add(path)
            for path, state in base_files.items():
                if path in paired:
                    continue
                before_text = self._baseline_text(state)
                if before_text is not None:
                    diff_results.append(FileDiffResult(path, "deleted", before_text, ""))
                    # 删除无需正文即可在变更集中表达，确定性已知即视为已呈现；
                    # 仅当正文不可读（``None``）时才标记存在未呈现的隐藏内容。
                    rendered_paths.add(path)
            for path in final_files:
                if path in paired:
                    continue
                after_text = _captured_text(validation.current[path])
                if after_text is not None:
                    diff_results.append(FileDiffResult(path, "added", "", after_text))
                    if after_text:
                        rendered_paths.add(path)

        stats = build_diff_stats(diff_results)
        # 删除类变更只报「哪个路径被删除」：被删文件正文属于回退事实（before-image），
        # 不进入变更集展示载荷。计数仍由 diff_results 统计，供面板显示删除行数。
        # 同一规则在单条快照投影里另有一处（``FileMutationService._display_patch`` 的
        # ``format="deleted"`` 分支），改动其中一处时须同步核对另一处。
        patches = [
            patch
            for result in diff_results
            if result.status != "deleted"
            and (patch := format_git_diff(result))
            and "\n@@ " in patch
        ]
        patch = "\n".join(item for item in patches if item)
        truncated = len(patch) > _PATCH_LIMIT
        return {
            "state": "verified",
            "additions": int(stats["total_insertions"]),
            "deletions": int(stats["total_deletions"]),
            "patch": None if truncated or not patch else patch,
            "truncated": truncated,
            "has_unrendered_changes": bool(changed_paths - rendered_paths),
        }

    def _baseline_text(self, state: dict[str, Any]) -> str | None:
        if not state.get("exists") or state.get("entry_type") != "file":
            return ""
        restore_ref = state.get("restore_ref")
        if not isinstance(restore_ref, str):
            raise ValueError("ChangeSet before-image reference is missing")
        # 删除类回退事实在 trash 暂存区，只报「哪个路径被删除」而不读取其正文，避免大文件读盘。
        if parse_trash_ref(restore_ref) is not None:
            return ""
        try:
            return _decode_text(self._blobs.read(restore_ref))
        except (UnicodeDecodeError, OSError, ValueError):
            return None

    @staticmethod
    def _action(group: _ChangeGroup, validation: _ValidatedGroup | None) -> str:
        if validation is None:
            return _stored_action(group.snapshots[-1]) if group.snapshots else "modified"
        baseline, final = validation.baseline, validation.final
        if all(state_matches(baseline[path], final[path]) for path in baseline):
            return "unchanged"
        base_existing = {path for path, state in baseline.items() if state.get("exists")}
        final_existing = {path for path, state in final.items() if state.get("exists")}
        if base_existing and final_existing and base_existing != final_existing:
            return "moved"
        if not base_existing and final_existing:
            return "added"
        if base_existing and not final_existing:
            return "deleted"
        return "modified"

    def _groups(self, task_id: int) -> list[_ChangeGroup]:
        snapshots = self._snapshots.list_any_by_task(task_id)
        buckets: dict[str, list[FileSnapshotRecord]] = defaultdict(list)
        for snapshot in snapshots:
            key = self._group_key(snapshot)
            buckets[key].append(snapshot)
        groups: list[_ChangeGroup] = []
        for key, records in buckets.items():
            segments: list[list[FileSnapshotRecord]] = []
            for record in records:
                if not segments or segments[-1][-1].status != record.status:
                    segments.append([record])
                else:
                    segments[-1].append(record)
            for segment in segments:
                ids = ",".join(str(record.id) for record in segment)
                digest = hashlib.sha256(f"{task_id}|{key}|{ids}".encode()).hexdigest()[:24]
                paths: set[str] = set()
                for record in segment:
                    envelope = cast(dict[str, Any], record.op_json)
                    entries = envelope.get("path_states", [])
                    for entry in entries if isinstance(entries, list) else []:
                        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                            paths.add(entry["path"])
                    if not paths:
                        paths.add(record.path)
                groups.append(
                    _ChangeGroup(
                        change_id=f"chg_{digest}",
                        key=key,
                        status=segment[0].status,
                        snapshots=tuple(segment),
                        paths=tuple(sorted(paths)),
                    )
                )
        groups.sort(key=lambda item: item.snapshots[0].seq)
        return groups

    @staticmethod
    def _group_key(snapshot: FileSnapshotRecord) -> str:
        envelope = cast(dict[str, Any], snapshot.op_json)
        states = envelope.get("path_states", [])
        identities = {
            entry.get("file_identity")
            for entry in states
            if isinstance(entry, dict) and isinstance(entry.get("file_identity"), str)
        }
        if len(identities) == 1:
            return f"identity:{next(iter(identities))}"
        return f"legacy:{snapshot.path}"

    def _task_and_root(self, task_id: int) -> tuple[TaskRecord, Path]:
        from app.service.depends import get_task_service, get_workspace_service

        task = get_task_service().get_task(task_id)
        workspace = get_workspace_service().get_workspace(task.workspace_id)
        root = Path(workspace.root_path).resolve()
        if not root.is_dir():
            raise ValueError(f"workspace root is unavailable for task {task_id}")
        return task, root

    @classmethod
    def _lock_scope(cls, root: Path, paths: Sequence[str]) -> tuple[Path, ...]:
        safe_paths = [
            path
            for path in paths
            if path
            and "\\" not in path
            and not Path(path).is_absolute()
            and ".." not in Path(path).parts
        ]
        candidates: set[Path] = set()
        for relative in safe_paths:
            target = root / Path(relative)
            candidates.add(target if target.parent == root else target.parent)
        minimal: list[Path] = []
        for target in sorted(candidates, key=lambda item: len(item.parts)):
            if any(target == existing or target.is_relative_to(existing) for existing in minimal):
                continue
            minimal.append(target)
        return PathResolver.with_workspace_ancestors(root, tuple(minimal))

    @classmethod
    def _path_locks(cls, root: Path, paths: Sequence[str]) -> AbstractContextManager[None]:
        lock_paths = cls._lock_scope(root, paths)
        return get_shared_file_path_lock_registry().acquire(str(root.resolve()), lock_paths)

    @staticmethod
    def _scope_covers(root: Path, scope: Sequence[Path], relative_path: str) -> bool:
        parsed = Path(relative_path)
        if (
            not relative_path
            or "\\" in relative_path
            or parsed.is_absolute()
            or ".." in parsed.parts
        ):
            return True
        candidate = _normalized_scope_path(relative_path)
        root_path = root.resolve()
        for locked in scope:
            try:
                prefix = locked.relative_to(root_path).as_posix()
            except ValueError:
                continue
            normalized = _normalized_scope_path(prefix)
            if candidate == normalized or candidate.startswith(f"{normalized}/"):
                return True
        return False

    def _capture_states(self, root: Path, paths: Iterable[str]) -> dict[str, dict[str, Any]]:
        return {path: capture_path(root, root / Path(path)).state for path in paths}

    def _restore_states(
        self,
        root: Path,
        states: dict[str, dict[str, Any]],
        *,
        expected_current: dict[str, dict[str, Any]] | None = None,
        defer_nonempty_absent_paths: set[str] | None = None,
        expected_directory_ids: dict[str, str] | None = None,
    ) -> None:
        deferred_paths = defer_nonempty_absent_paths or set()
        expected_ids = expected_directory_ids or {}
        for path, expected_id in expected_ids.items():
            if directory_identity(root / Path(path)) != expected_id:
                raise _RevertExternalEdit(f"created parent directory identity changed: {path}")
        present = sorted(
            ((path, state) for path, state in states.items() if state.get("exists")),
            key=lambda item: (
                item[0].count("/"),
                0 if item[1].get("entry_type") == "directory" else 1,
                item[0],
            ),
        )
        absent = sorted(
            ((path, state) for path, state in states.items() if not state.get("exists")),
            key=lambda item: (-item[0].count("/"), item[0]),
        )
        for path, state in (*present, *absent):
            if path in expected_ids and directory_identity(root / Path(path)) != expected_ids[path]:
                raise _RevertExternalEdit(f"created parent directory identity changed: {path}")
            if expected_current is not None:
                current = capture_path(root, root / Path(path)).state
                if not state_matches(current, expected_current[path]):
                    raise _RevertExternalEdit(f"workspace changed during ChangeSet Revert: {path}")
            try:
                restore_path_state(
                    root,
                    path,
                    state,
                    self._blobs,
                    expected_current=(expected_current or {}).get(path),
                    expected_directory_identity=expected_ids.get(path),
                )
            except WorkspacePathChanged as exc:
                raise _RevertExternalEdit(
                    f"workspace changed during ChangeSet Revert: {path}"
                ) from exc
            except OSError as exc:
                if (
                    path in deferred_paths
                    and not state.get("exists")
                    and exc.errno in {errno.ENOTEMPTY, errno.EEXIST}
                ):
                    continue
                if not state.get("exists") and exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                    raise _RevertExternalEdit(
                        f"created parent directory is no longer empty: {path}"
                    ) from exc
                raise

    @staticmethod
    def _result(change_id: str, outcome: str, reason_code: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"change_id": change_id, "outcome": outcome}
        if reason_code:
            result["reason_code"] = reason_code
        return result


def _captured_text(captured: CapturedPath) -> str | None:
    if not captured.state.get("exists") or captured.state.get("entry_type") != "file":
        return ""
    if captured.content is None:
        return None
    return _decode_text(captured.content)


def _decode_text(content: bytes) -> str | None:
    """Decode UTF-8 text while treating NUL-containing payloads as binary."""

    if b"\x00" in content:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _empty_net_diff(state: str) -> dict[str, Any]:
    return {
        "state": state,
        "additions": 0,
        "deletions": 0,
        "patch": None,
        "truncated": False,
        "has_unrendered_changes": False,
    }


def _stored_action(snapshot: FileSnapshotRecord) -> str:
    """Return the displayed action represented by the typed operation envelope."""
    envelope = cast(dict[str, Any], snapshot.op_json)
    operation = envelope.get("operation")
    operation_type = operation.get("type") if isinstance(operation, dict) else None
    action_by_type = {
        "ADD": "added",
        "DELETE": "deleted",
        "UPDATE": "modified",
        "MOVE": "moved",
        "PREPARED": "prepared",
    }
    if isinstance(operation_type, str) and operation_type in action_by_type:
        return action_by_type[operation_type]
    return "modified"


def _overlaps(left: str, right: str) -> bool:
    left_parts = Path(os.path.normcase(left)).parts
    right_parts = Path(os.path.normcase(right)).parts
    common = min(len(left_parts), len(right_parts))
    return left_parts[:common] == right_parts[:common]


def _normalized_scope_path(path: str) -> str:
    """Normalize workspace-relative paths with the host filesystem's case rules."""

    return os.path.normcase(path.replace("/", os.sep)).replace("\\", "/")


def _group_side_effect_directory_ids(group: _ChangeGroup) -> dict[str, str]:
    """Collect already-validated created-parent identities from a file group."""
    identities: dict[str, str] = {}
    for snapshot in group.snapshots:
        envelope = cast(dict[str, Any], snapshot.op_json)
        side_effects = envelope.get("side_effect_states", [])
        if not isinstance(side_effects, list):
            continue
        for item in side_effects:
            if not isinstance(item, dict):
                continue
            path, identity = item.get("path"), item.get("directory_id")
            if isinstance(path, str) and isinstance(identity, str):
                identities[path] = identity
    return identities
