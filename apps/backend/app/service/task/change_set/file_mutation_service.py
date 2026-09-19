"""Persist before-images and reconcile actual structured file-tool mutations."""

from __future__ import annotations

import os
import posixpath
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, cast

from app.config.logging.logger import log
from app.core.tools.guard.file_mutation_guard import FileMutationGuard, mutation_guard_scope
from app.core.tools.guard.file_mutation_state import (
    CapturedPath,
    UnsupportedWorkspaceEntry,
    capture_path,
    directory_identity,
    is_relative_ancestor,
    state_matches,
)
from app.core.tools.guard.trash_staging import (
    parse_trash_ref,
    trash_restore_ref,
    trash_root_for,
)
from app.core.tools.schemas import ToolExecutionContext, ToolObservation
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_handler.patch_write.patch_diff import (
    FileDiffResult,
    format_git_diff,
)
from app.models.file_snapshot_json import (
    FileSnapshotDisplayPatchJson,
    FileSnapshotMutationJson,
    FileSnapshotOpJson,
    FileSnapshotPathStateJson,
    FileSnapshotSideEffectStateJson,
    FileSnapshotStagingJson,
    FileSnapshotStateJson,
)
from app.models.file_snapshot_record import FileSnapshotRecord
from app.service.task.change_set.blob_garbage_collector import ChangeSetBlobGarbageCollector
from app.service.task.change_set.blob_store import ChangeSetBlobStore
from app.storage.crud.file_snapshot_crud import FileSnapshotCrud


class FileMutationService:
    """Persist before-images before file tools and save the state actually left on disk.

    Prepared rows stay hidden from ChangeSet queries. After cancellation or a backend
    restart, startup compares those rows with the workspace and finalizes the observed
    delta; it never restores workspace files automatically.
    """

    _TRACKED_TOOLS = frozenset(
        {"write_file", "replace", "patch_write", "apply_patch", "delete_file", "move_file"}
    )

    def __init__(
        self,
        *,
        snapshot_crud: FileSnapshotCrud | None = None,
        blob_store: ChangeSetBlobStore | None = None,
    ) -> None:
        self._snapshots = snapshot_crud or FileSnapshotCrud()
        self._blobs = blob_store or ChangeSetBlobStore()
        self._blob_gc = ChangeSetBlobGarbageCollector(self._blobs, self._snapshots)

    def execute_tool_mutation(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        execution_context: ToolExecutionContext,
        tool_call_id: str,
        write_paths: Sequence[Path],
        execute: Callable[[], ToolObservation],
    ) -> ToolObservation:
        """Prepare durable before-images, run a file handler, and record its actual delta."""
        if tool_name not in self._TRACKED_TOOLS or not write_paths:
            return execute()

        root = execution_context.workspace_root.resolve()
        blocked = self._unfinalized_paths_for_workspace(execution_context.workspace_id)
        requested = [
            Path(path).relative_to(root).as_posix()
            for path in write_paths
            if Path(path).is_relative_to(root)
        ]
        if any(_paths_overlap(path, pending) for path in requested for pending in blocked):
            return _mutation_error(tool_name, "an earlier file change is still being reconciled")

        operation_id = f"mut_{uuid.uuid4().hex}"
        staged: list[tuple[str, str]] = []
        prepared = False
        try:
            target_captures = {
                item.path: item
                for item in self._capture_before(
                    root, write_paths, read_content=(tool_name != "delete_file")
                )
            }
            snapshot_paths = set(target_captures)
            implicit_captures = self._capture_missing_write_parents(root, write_paths)
            implicit_paths = {item.path for item in implicit_captures}
            before_captures = {**target_captures, **{item.path: item for item in implicit_captures}}
            before, staged = self._stage_before_images(
                list(before_captures.values()),
                tool_name=tool_name,
                operation_id=operation_id,
            )
            move_pairs: list[list[str]] = [
                list(pair) for pair in self._move_pairs(root, tool_name, arguments)
            ]
            staging_refs: list[FileSnapshotStagingJson] = [
                {"id": item, "sha256": digest} for item, digest in staged
            ]
            manifest: dict[str, Any] = {
                "kind": "agent_mutation",
                "mutation_id": operation_id,
                "workspace_id": execution_context.workspace_id,
                "task_id": execution_context.task_id,
                "run_id": execution_context.run_id,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "paths_before": before,
                "paths_after": {},
                "snapshot_paths": sorted(snapshot_paths),
                "implicit_directory_paths": sorted(implicit_paths),
                "move_pairs": move_pairs,
                "staging": staging_refs,
            }
            persisted_manifest: FileSnapshotMutationJson = {
                "kind": "agent_mutation",
                "workspace_id": execution_context.workspace_id,
                "snapshot_paths": sorted(snapshot_paths),
                "implicit_directory_paths": sorted(implicit_paths),
                "move_pairs": move_pairs,
                "staging": staging_refs,
            }
            prepared_records = self._build_prepared_records(
                task_id=execution_context.task_id,
                run_id=execution_context.run_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                operation_id=operation_id,
                manifest=manifest,
                persisted_manifest=persisted_manifest,
                before=before,
            )
            self._snapshots.save_batch_with_sequence(execution_context.task_id, prepared_records)
            prepared = True
            try:
                for staging_id, digest in staged:
                    self._blobs.publish(staging_id, digest)
            except Exception:
                # No file handler has run yet, so a known publication failure can
                # discard its prepared rows and let the caller retry immediately.
                # If SQLite cannot remove them, keep their staging refs for startup
                # reconciliation instead of leaving dangling before-image pointers.
                try:
                    self._snapshots.delete_prepared(operation_id)
                except Exception:
                    log.exception(
                        "file_mutation_prepared_cleanup_failed",
                        extra={
                            "msg": "文件操作尚未开始，但预写快照清理失败；保留引用对象供启动对账",
                            "data": {
                                "task_id": execution_context.task_id,
                                "operation_id": operation_id,
                            },
                        },
                    )
                self._collect_garbage_best_effort()
                raise

            # External editors do not participate in the process-wide path lock.
            current = self._capture_states(root, list(before))
            if any(not state_matches(current[path], before[path]) for path in before):
                self._snapshots.delete_prepared(operation_id)
                self._cleanup_staging(staged)
                self._collect_garbage_best_effort()
                return _mutation_error(tool_name, "workspace file changed before the write started")
            guard = FileMutationGuard(
                manifest,
                root,
                capture_states=self._capture_states,
                trash_root=trash_root_for(root, operation_id)
                if tool_name == "delete_file"
                else None,
            )
            try:
                with mutation_guard_scope(guard):
                    observation = execute()
            except Exception:
                log.exception(
                    "file_mutation_handler_raised",
                    extra={
                        "msg": "文件工具执行异常；将把已落盘的实际变化收录到变更集",
                        "data": {
                            "task_id": execution_context.task_id,
                            "run_id": execution_context.run_id,
                            "operation_id": operation_id,
                            "tool_name": tool_name,
                        },
                    },
                )
                observation = _mutation_error(tool_name, "file tool execution failed")

            self._finalize_observed_mutation(
                operation_id=operation_id,
                root=root,
                manifest=manifest,
                before=before,
                before_captures=before_captures,
                arguments=arguments,
            )
            self._cleanup_staging(staged)
            self._collect_garbage_best_effort()
            return observation
        except Exception as exc:
            self._cleanup_staging(staged)
            event = (
                "file_mutation_snapshot_finalize_failed"
                if prepared
                else "file_mutation_snapshot_prepare_failed"
            )
            message = (
                "文件变化快照收口失败；保留预写快照供后端启动时对账"
                if prepared
                else "无法持久化文件操作前快照，未执行文件工具"
            )
            log.exception(
                event,
                extra={
                    "msg": message,
                    "data": {
                        "task_id": execution_context.task_id,
                        "run_id": execution_context.run_id,
                        "operation_id": operation_id,
                        "tool_name": tool_name,
                    },
                },
            )
            return _mutation_error(tool_name, f"ChangeSet could not record this write: {exc}")

    def reconcile_prepared_snapshots(self) -> list[str]:
        """Finalize interrupted operations from actual disk state without restoring files.

        Runs after orphan Runs become cancelled and before backend readiness. Unchanged
        operations are removed; changed operations become ChangeSets for the exact state on
        disk. Unreadable operations remain hidden and their paths are returned to block
        overlapping mutations until a later startup can retry.
        """
        unresolved: set[str] = set()
        groups: dict[str, list[FileSnapshotRecord]] = {}
        for record in self._snapshots.list_unfinalized():
            if record.mutation_state == "prepared":
                groups.setdefault(record.mutation_id, []).append(record)
        for mutation_id, records in groups.items():
            try:
                manifest = next(
                    (record.mutation_json for record in records if record.mutation_json),
                    None,
                )
                if manifest is None:
                    raise ValueError("prepared snapshot has no recovery manifest")
                self._reconcile_prepared_operation(mutation_id, records, manifest)
            except Exception:
                log.exception(
                    "file_mutation_snapshot_reconcile_failed",
                    extra={
                        "msg": "预写文件快照暂时无法对账；该操作保持隐藏并阻止重叠写入",
                        "data": {
                            "task_id": records[0].task_id if records else None,
                            "mutation_id": mutation_id,
                        },
                    },
                )
                for record in records:
                    record_manifest = _json_object(record.mutation_json)
                    unresolved_paths = {
                        path
                        for path in record_manifest.get("snapshot_paths", [])
                        if isinstance(path, str)
                    }
                    unresolved_paths.update(
                        path
                        for path in record_manifest.get("implicit_directory_paths", [])
                        if isinstance(path, str)
                    )
                    unresolved.update(unresolved_paths or {record.path})
        self._cleanup_orphan_staging()
        self._collect_garbage_best_effort()
        return sorted(unresolved)

    def _build_prepared_records(
        self,
        *,
        task_id: int,
        run_id: int,
        tool_name: str,
        tool_call_id: str,
        operation_id: str,
        manifest: dict[str, Any],
        persisted_manifest: FileSnapshotMutationJson,
        before: dict[str, dict[str, Any]],
    ) -> list[FileSnapshotRecord]:
        """Build hidden per-path rows that reserve sequence slots before file effects."""
        paths = sorted(set(manifest["snapshot_paths"]) | set(manifest["implicit_directory_paths"]))
        if not paths:
            raise ValueError("file mutation snapshot has no affected workspace paths")
        owners = self._file_identity_owners(task_id)
        records: list[FileSnapshotRecord] = []
        for offset, path in enumerate(paths):
            identity = owners.get(path) or f"fi_{uuid.uuid4().hex}"
            envelope: FileSnapshotOpJson = {
                "operation": {"type": "PREPARED", "operation_id": operation_id, "tool": tool_name},
                "path_states": [
                    {
                        "file_identity": identity,
                        "path": path,
                        "before": cast(FileSnapshotStateJson, before[path]),
                        "after": cast(FileSnapshotStateJson, before[path]),
                    }
                ],
            }
            records.append(
                FileSnapshotRecord(
                    task_id=task_id,
                    run_id=run_id,
                    tool_call_id=tool_call_id,
                    mutation_id=operation_id,
                    mutation_state="prepared",
                    mutation_json=persisted_manifest if offset == 0 else None,
                    path=path,
                    op_json=envelope,
                    seq=offset,
                )
            )
        return records

    def _finalize_observed_mutation(
        self,
        *,
        operation_id: str,
        root: Path,
        manifest: dict[str, Any],
        before: dict[str, dict[str, Any]],
        before_captures: dict[str, CapturedPath],
        arguments: Mapping[str, object],
    ) -> list[FileSnapshotRecord]:
        """Save the actual observed delta, including partial writes left by a failed tool."""
        after_captures = self._capture_live(root, list(before))
        after = {path: capture.state for path, capture in after_captures.items()}
        changed_paths = [path for path in before if not state_matches(before[path], after[path])]
        snapshot_paths = set(manifest["snapshot_paths"])
        changed_visible = [path for path in changed_paths if path in snapshot_paths]
        implicit_paths = set(manifest["implicit_directory_paths"])
        if not changed_visible and changed_paths:
            changed_visible = [path for path in changed_paths if path in implicit_paths]
        records = self._build_snapshot_records(
            task_id=int(manifest["task_id"]),
            run_id=int(manifest["run_id"]),
            tool_name=str(manifest["tool_name"]),
            tool_call_id=str(manifest["tool_call_id"]),
            operation_id=operation_id,
            arguments=arguments,
            workspace_root=root,
            before=before,
            after=after,
            before_captures=before_captures,
            after_captures=after_captures,
            changed_paths=changed_visible,
            implicit_directory_paths=implicit_paths
            if changed_visible and changed_visible[0] in snapshot_paths
            else set(),
            move_pairs=[tuple(pair) for pair in manifest.get("move_pairs", [])],
        )
        self._snapshots.finalize_prepared(operation_id, records)
        if changed_paths and not changed_visible:
            log.warning(
                "file_mutation_unrepresented_side_effect",
                extra={
                    "msg": "文件操作只改变了未映射为变更项的路径",
                    "data": {
                        "operation_id": operation_id,
                        "changed_path_count": len(changed_paths),
                    },
                },
            )
        return records

    def _reconcile_prepared_operation(
        self,
        mutation_id: str,
        records: list[FileSnapshotRecord],
        manifest: FileSnapshotMutationJson,
    ) -> None:
        """Publish referenced before-images and finalize an interrupted operation from disk."""
        if manifest.get("kind") != "agent_mutation" or not records:
            raise ValueError("prepared file snapshot manifest does not match its rows")
        first = records[0]
        if any(
            record.mutation_id != mutation_id
            or record.task_id != first.task_id
            or record.run_id != first.run_id
            for record in records
        ):
            raise ValueError("prepared file snapshot rows do not belong to one operation")
        first_envelope = _json_object(first.op_json)
        operation = first_envelope.get("operation")
        tool_name = operation.get("tool") if isinstance(operation, dict) else None
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("prepared file snapshot tool name is missing")
        for item in manifest.get("staging", []):
            if not isinstance(item, dict):
                raise ValueError("prepared snapshot staging entry is malformed")
            staging_id, digest = item.get("id"), item.get("sha256")
            if not isinstance(staging_id, str) or not isinstance(digest, str):
                raise ValueError("prepared snapshot staging entry is malformed")
            self._blobs.publish(staging_id, digest)
        root = self._workspace_root(int(manifest["workspace_id"]))
        before: dict[str, dict[str, Any]] = {}
        before_captures: dict[str, CapturedPath] = {}
        for record in records:
            envelope = cast(dict[str, Any], record.op_json)
            states = envelope.get("path_states")
            if not isinstance(states, list) or len(states) != 1:
                raise ValueError("prepared path snapshot is malformed")
            state = states[0]
            path, previous = state.get("path"), state.get("before")
            if not isinstance(path, str) or not isinstance(previous, dict):
                raise ValueError("prepared path snapshot is malformed")
            before[path] = previous
        expected_paths = set(manifest.get("snapshot_paths", [])) | set(
            manifest.get("implicit_directory_paths", [])
        )
        if set(before) != expected_paths:
            raise ValueError("prepared path manifest does not match its reserved rows")
        for path, state in before.items():
            content = None
            restore_ref = state.get("restore_ref")
            if state.get("exists") and state.get("entry_type") in {"file", "symlink"}:
                if not isinstance(restore_ref, str):
                    raise ValueError("prepared snapshot before-image is missing")
                # trash 引用指向落盘暂存副本，不读入内存（删除类回退事实）。
                if parse_trash_ref(restore_ref) is None:
                    content = self._blobs.read(restore_ref)
            before_captures[path] = CapturedPath(path, state, content)
        finalization_manifest = {
            **manifest,
            "task_id": first.task_id,
            "run_id": first.run_id,
            "tool_name": tool_name,
            "tool_call_id": first.tool_call_id,
        }
        self._finalize_observed_mutation(
            operation_id=mutation_id,
            root=root,
            manifest=finalization_manifest,
            before=before,
            before_captures=before_captures,
            arguments={},
        )

    def _unfinalized_paths_for_workspace(self, workspace_id: int) -> list[str]:
        paths: set[str] = set()
        from app.service.depends import get_task_service

        for record in self._snapshots.list_unfinalized():
            manifest = _json_object(record.mutation_json)
            if manifest.get("workspace_id") != workspace_id:
                task = get_task_service().get_task(record.task_id)
                if task.workspace_id != workspace_id:
                    continue
            record_paths: set[str] = set()
            if manifest.get("kind") == "agent_mutation":
                record_paths.update(
                    path for path in manifest.get("snapshot_paths", []) if isinstance(path, str)
                )
                record_paths.update(
                    path
                    for path in manifest.get("implicit_directory_paths", [])
                    if isinstance(path, str)
                )
            else:
                envelope = _json_object(record.op_json)
                for field in ("path_states", "side_effect_states"):
                    record_paths.update(
                        state["path"]
                        for state in envelope.get(field, [])
                        if isinstance(state, dict) and isinstance(state.get("path"), str)
                    )
            paths.update(record_paths or {record.path})
        return sorted(paths)

    def _capture_before(
        self,
        root: Path,
        write_paths: Sequence[Path],
        *,
        read_content: bool = True,
    ) -> list[CapturedPath]:
        """Capture explicit write targets before the tool mutates them.

        参数:
            root: 工作区根路径。
            write_paths: 处理器将要改写的显式目标路径。
            read_content: 是否读取并返回普通文件完整字节；``False`` 时只做流式
                ``sha256``，用于删除等无需正文即可回退的场景（避免大文件读入内存）。
        """
        captures: dict[str, CapturedPath] = {}
        for path in write_paths:
            item = capture_path(root, path, read_content=read_content)
            captures.setdefault(item.path, item)
        return [
            captures[path] for path in sorted(captures, key=lambda value: (value.count("/"), value))
        ]

    @staticmethod
    def _capture_missing_write_parents(
        root: Path,
        write_paths: Sequence[Path],
    ) -> list[CapturedPath]:
        """Capture absent parent directories that file handlers may create."""
        root = root.resolve()
        missing: dict[str, CapturedPath] = {}
        for path in write_paths:
            parent = Path(os.path.abspath(path)).parent
            while parent != root and parent.is_relative_to(root):
                if not os.path.lexists(parent):
                    captured = capture_path(root, parent)
                    missing[captured.path] = captured
                parent = parent.parent
        return [
            missing[path] for path in sorted(missing, key=lambda value: (value.count("/"), value))
        ]

    def _stage_before_images(
        self,
        captures: Sequence[CapturedPath],
        *,
        tool_name: str,
        operation_id: str,
    ) -> tuple[dict[str, dict[str, Any]], list[tuple[str, str]]]:
        """Stage exact file and symbolic-link before-images for durable snapshots.

        ``delete_file`` 的删除目标不写 blob、也不要求正文在内存中：其回退事实是落盘删除前
        移入 trash 暂存区的副本，``restore_ref`` 指向 ``trash:<operation_id>/<relative>``，
        避免对大文件/二进制文件整读内存与重复拷贝磁盘。其余文件与符号链接仍按内容寻址
        blob 暂存。
        """
        before: dict[str, dict[str, Any]] = {}
        staged: list[tuple[str, str]] = []
        is_delete = tool_name == "delete_file"
        try:
            for captured in captures:
                state = dict(captured.state)
                if state.get("exists") and state.get("entry_type") == "file":
                    if is_delete:
                        # 删除走 trash 暂存，不占内存、不写 blob。
                        state["restore_ref"] = trash_restore_ref(operation_id, captured.path)
                    elif captured.content is None:
                        raise UnsupportedWorkspaceEntry("file before-image is unavailable")
                    else:
                        staging_id, digest = self._blobs.stage(captured.content)
                        state["restore_ref"] = f"sha256:{digest}"
                        staged.append((staging_id, digest))
                elif state.get("entry_type") == "symlink":
                    target = state.get("target")
                    if not isinstance(target, str):
                        raise UnsupportedWorkspaceEntry("symbolic link target is unavailable")
                    staging_id, digest = self._blobs.stage(os.fsencode(target))
                    state["restore_ref"] = f"sha256:{digest}"
                    staged.append((staging_id, digest))
                before[captured.path] = state
        except BaseException:
            self._cleanup_staging(staged)
            raise
        return before, staged

    @staticmethod
    def _capture_live(root: Path, relative_paths: Sequence[str]) -> dict[str, CapturedPath]:
        return {relative: capture_path(root, root / Path(relative)) for relative in relative_paths}

    @classmethod
    def _capture_states(
        cls,
        root: Path,
        relative_paths: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        return {path: item.state for path, item in cls._capture_live(root, relative_paths).items()}

    def _build_snapshot_records(
        self,
        *,
        task_id: int,
        run_id: int,
        tool_name: str,
        tool_call_id: str,
        operation_id: str,
        arguments: Mapping[str, object],
        workspace_root: Path,
        before: dict[str, dict[str, Any]],
        after: dict[str, dict[str, Any]],
        before_captures: dict[str, CapturedPath],
        after_captures: dict[str, CapturedPath],
        changed_paths: Sequence[str],
        implicit_directory_paths: set[str],
        move_pairs: Sequence[tuple[str, str]] | None = None,
    ) -> list[FileSnapshotRecord]:
        """Project observed path deltas into reversible file snapshot records."""
        if not changed_paths:
            return []
        owners = self._file_identity_owners(task_id)
        moves = (
            self._move_pairs(workspace_root, tool_name, arguments)
            if move_pairs is None
            else list(move_pairs)
        )
        specs: list[tuple[str, list[str], str]] = []
        covered: set[str] = set()
        for source, destination in moves:
            source_removed = before[source].get("exists") and not after[source].get("exists")
            destination_added = (
                not before[destination].get("exists") and after[destination].get("exists")
            )
            if source_removed and destination_added:
                specs.append(("MOVE", [source, destination], "moved"))
                covered.update((source, destination))
        for path in changed_paths:
            if path in covered:
                continue
            old, new = before[path], after[path]
            if old.get("exists") and not new.get("exists"):
                operation_type, action = "DELETE", "deleted"
            elif not old.get("exists") and new.get("exists"):
                operation_type, action = "ADD", "added"
            else:
                operation_type, action = "UPDATE", "modified"
            specs.append((operation_type, [path], action))

        records: list[FileSnapshotRecord] = []
        for offset, (operation_type, paths, action) in enumerate(specs):
            identity = owners.get(paths[0]) or (owners.get(paths[1]) if len(paths) > 1 else None)
            identity = identity or f"fi_{uuid.uuid4().hex}"
            path_states: list[FileSnapshotPathStateJson] = [
                {
                    "file_identity": owners.get(path, identity),
                    "path": path,
                    "before": cast(FileSnapshotStateJson, before[path]),
                    "after": cast(FileSnapshotStateJson, after[path]),
                }
                for path in paths
                if path in before and path in after
            ]
            if len(path_states) != len(paths):
                continue
            display_patch = self._display_patch(paths, action, before_captures, after_captures)
            envelope: FileSnapshotOpJson = {
                "operation": {
                    "type": cast(
                        Literal["ADD", "DELETE", "UPDATE", "MOVE", "PREPARED"], operation_type
                    ),
                    "operation_id": operation_id,
                    "tool": tool_name,
                },
                "path_states": path_states,
                "display_patch": display_patch,
            }
            side_effect_states: list[FileSnapshotSideEffectStateJson] = []
            for directory_path in sorted(implicit_directory_paths):
                if not any(is_relative_ancestor(directory_path, path) for path in paths):
                    continue
                if not after.get(directory_path, {}).get("exists"):
                    continue
                identity_value = directory_identity(workspace_root / Path(directory_path))
                if identity_value is not None:
                    side_effect_states.append(
                        {
                            "path": directory_path,
                            "before": cast(FileSnapshotStateJson, before[directory_path]),
                            "after": cast(FileSnapshotStateJson, after[directory_path]),
                            "directory_id": identity_value,
                        }
                    )
            if side_effect_states:
                envelope["side_effect_states"] = side_effect_states
            records.append(
                FileSnapshotRecord(
                    task_id=task_id,
                    run_id=run_id,
                    tool_call_id=tool_call_id,
                    path=paths[0],
                    op_json=envelope,
                    seq=offset,
                )
            )
        return records

    def _file_identity_owners(self, task_id: int) -> dict[str, str]:
        """Return each path's current logical file identity from finalized snapshots."""
        owners: dict[str, str] = {}
        for snapshot in self._snapshots.list_any_by_task(task_id):
            envelope = cast(dict[str, Any], snapshot.op_json)
            for state in envelope.get("path_states", []):
                if not isinstance(state, dict):
                    continue
                identity, path = state.get("file_identity"), state.get("path")
                if not isinstance(identity, str) or not isinstance(path, str):
                    continue
                effective = (
                    state.get("before") if snapshot.status == "reverted" else state.get("after")
                )
                if isinstance(effective, dict) and effective.get("exists"):
                    owners[path] = identity
                else:
                    owners.pop(path, None)
        return owners

    @staticmethod
    def _move_pairs(
        root: Path,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> list[tuple[str, str]]:
        """Extract MOVE path pairs without persisting the complete patch input."""
        if tool_name != "move_file":
            return []
        source_path = arguments.get("source_path")
        destination_path = arguments.get("destination_path")
        if not isinstance(source_path, str) or not isinstance(destination_path, str):
            return []
        try:
            return [
                (
                    _workspace_relative(root, source_path),
                    _workspace_relative(root, destination_path),
                )
            ]
        except (OSError, ValueError):
            return []

    @staticmethod
    def _display_patch(
        paths: Sequence[str],
        action: str,
        before_captures: dict[str, CapturedPath],
        after_captures: dict[str, CapturedPath],
    ) -> FileSnapshotDisplayPatchJson:
        """Build the display descriptor for one observed operation.

        Deletions carry no content (``format="deleted"``): the removed text is a revert fact kept
        in the before-image, not a display fact. Moves keep rename metadata, non-decodable
        targets degrade to ``binary``, and everything else keeps a bounded unified diff.

        同一条规则在任务级净差投影里另有一处（``TaskChangeSetService._net_diff`` 过滤 deleted
        结果），改动本函数时须同步核对那边。
        """
        if action == "deleted":
            # 删除内容属于回退事实（before-image 已持久化在 blob store），不进入展示载荷：
            # 变更集界面只需要知道「哪个路径被删除」。这里不再生成文本差异，避免把被删文件
            # 正文写进 file_snapshots.op_json。
            return {"format": "deleted", "text": "", "truncated": False}
        if len(paths) == 2 and action == "moved":
            patch = "\n".join(
                (
                    f"diff --git a/{paths[0]} b/{paths[1]}",
                    "similarity index 100%",
                    f"rename from {paths[0]}",
                    f"rename to {paths[1]}",
                )
            )
            return {"format": "rename", "text": patch, "truncated": False}
        path = paths[0]
        try:
            before_text = _captured_text(before_captures[path])
            after_text = _captured_text(after_captures[path])
        except (UnicodeDecodeError, ValueError, KeyError):
            return {"format": "binary", "text": "", "truncated": False}
        result = FileDiffResult(path=path, status=action, before=before_text, after=after_text)
        patch = format_git_diff(result)
        limit = 16_000
        truncated = len(patch) > limit
        return {
            "format": "unified",
            "text": patch[:limit] if not truncated else "",
            "truncated": truncated,
        }

    def _workspace_root(self, workspace_id: int) -> Path:
        """Resolve a workspace root for startup snapshot reconciliation."""
        from app.service.depends import get_workspace_service

        workspace = get_workspace_service().get_workspace(workspace_id)
        root = Path(workspace.root_path).resolve()
        if not root.is_dir():
            raise ValueError(f"workspace root is unavailable for workspace {workspace_id}")
        return root

    def _cleanup_staging(self, staged: Sequence[tuple[str, str]]) -> None:
        """Remove only staging files no durable prepared snapshot still references."""
        active: set[str] = set()
        try:
            for record in self._snapshots.list_unfinalized():
                manifest = _json_object(record.mutation_json)
                for item in manifest.get("staging", []):
                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                        active.add(item["id"])
        except Exception:
            log.warning(
                "change_set_staging_reference_check_failed",
                extra={
                    "msg": "无法确认 ChangeSet 临时对象是否仍被预写快照引用，暂时保留对象",
                    "data": {"staging_count": len(staged)},
                },
                exc_info=True,
            )
            return
        for staging_id, _ in staged:
            if staging_id in active:
                continue
            try:
                self._blobs.discard_staging(staging_id)
            except OSError:
                log.warning(
                    "change_set_staging_cleanup_failed",
                    extra={
                        "msg": "ChangeSet 临时对象清理失败；后端启动时会清理未引用对象",
                        "data": {"staging_id": staging_id},
                    },
                    exc_info=True,
                )

    def _cleanup_orphan_staging(self) -> None:
        """Remove staging files not referenced by any durable prepared snapshot."""
        active: set[str] = set()
        for record in self._snapshots.list_unfinalized():
            manifest = _json_object(record.mutation_json)
            for item in manifest.get("staging", []):
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    active.add(item["id"])
        for path in self._blobs.staging_dir.iterdir():
            if path.is_file() and path.name not in active:
                path.unlink(missing_ok=True)

    def _collect_garbage_best_effort(self) -> None:
        """Collect unreferenced blobs without allowing diagnostics to fail a mutation."""
        try:
            removed = self._blob_gc.collect()
        except Exception:
            log.exception(
                "change_set_blob_gc_failed",
                extra={"msg": "ChangeSet before-image 清理失败；后续收口操作会重试", "data": {}},
            )
            return
        if removed:
            log.info(
                "change_set_blob_gc_completed",
                extra={
                    "msg": "已清理不再被快照引用的 ChangeSet 对象",
                    "data": {"removed_count": removed},
                },
            )


def _captured_text(captured: CapturedPath) -> str:
    """Decode exact captured UTF-8 file bytes for the unified-diff projector."""
    if not captured.state.get("exists"):
        return ""
    if captured.state.get("entry_type") != "file" or captured.content is None:
        raise ValueError("non-file path has no textual diff")
    return captured.content.decode("utf-8")


def _json_object(value: object) -> dict[str, Any]:
    """Return a mapping view when a record contains a JSON object."""

    return value if isinstance(value, dict) else {}


def _paths_overlap(left: str, right: str) -> bool:
    """Return whether two workspace-relative paths have an ancestor relation."""
    left_parts = Path(os.path.normcase(left)).parts
    right_parts = Path(os.path.normcase(right)).parts
    common = min(len(left_parts), len(right_parts))
    return left_parts[:common] == right_parts[:common]


def _workspace_relative(root: Path, raw_path: str) -> str:
    """Normalize a patch path to the captured workspace-relative key."""
    value = Path(raw_path)
    absolute = value if value.is_absolute() else root / value
    resolved = absolute.resolve(strict=False)
    return posixpath.normpath(resolved.relative_to(root).as_posix())


def _mutation_error(tool_name: str, reason: str) -> ToolObservation:
    return tool_error(
        tool_name,
        reason,
        reason=(
            "the file change could not be recorded safely; inspect the workspace and retry "
            "after resolving the storage or file-state issue."
        ),
        retryable=True,
    )
