"""Task ChangeSet net-diff and whole-baseline-revert contract tests."""

import hashlib
import json
import os
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import inspect

import app.service.task.change_set.blob_store as blob_store_module
from app.config.settings import Settings
from app.core.tools.guard.file_mutation_guard import (
    before_file_move,
)
from app.core.tools.guard.file_resource_paths import FileResourcePaths
from app.core.tools.guard.file_tool_state_coordinator import (
    FileToolExecutionPlan,
    FileToolStateCoordinator,
)
from app.core.tools.schemas import ToolExecutionContext, ToolObservation
from app.core.tools.tool_handler.delete_tool import DeleteTool
from app.core.tools.tool_handler.patch_write.atomic_write import atomic_write_text
from app.models.file_snapshot_record import FileSnapshotRecord
from app.service.depends import (
    close_service_dependencies,
    get_conversation_run_crud,
    get_file_snapshot_crud,
    get_task_crud,
    get_workspace_crud,
)
from app.service.task.change_set.blob_garbage_collector import ChangeSetBlobGarbageCollector
from app.service.task.change_set.blob_store import ChangeSetBlobStore
from app.service.task.change_set.file_mutation_service import FileMutationService
from app.service.task.change_set.file_state import CapturedPath
from app.service.task.change_set.task_change_set_service import TaskChangeSetService
from app.service.task.conversation_run_service import ConversationRunService
from app.service.task.conversation_run_state_service import ConversationRunStateService
from app.storage.store_engines import init_storage, main_session_factory
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


@pytest.fixture
def change_set_env(tmp_path: Path) -> Iterator[tuple[Path, int, int, ChangeSetBlobStore]]:
    close_service_dependencies()
    storage = tmp_path / "storage"
    storage.mkdir()
    Settings.override(
        DATABASE_FILE=storage / "app.sqlite3",
        LOG_DATABASE_FILE=storage / "logs.sqlite3",
        CHECKPOINT_FILE=storage / "checkpoints.sqlite3",
        LOG_DIR=storage / "logs",
    )
    init_storage()
    task_runtime_spaces.close()
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    workspace = get_workspace_crud().create("test", str(workspace_root))
    task = get_task_crud().create(workspace.id, "test task")
    run = get_conversation_run_crud().create(task.id, "test", status="cancelled")
    blob_store = ChangeSetBlobStore(storage / "change_set")
    yield workspace_root, task.id, run.id, blob_store
    task_runtime_spaces.close()
    close_service_dependencies()


def _state(content: bytes | None, restore_ref: str | None = None) -> dict:
    if content is None:
        return {"exists": False}
    result = {
        "exists": True,
        "entry_type": "file",
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    if restore_ref is not None:
        result["restore_ref"] = restore_ref
    return result


def _put_blob(store: ChangeSetBlobStore, content: bytes) -> str:
    """测试夹具：写入一个 ChangeSet 对象并返回其 restore_ref。

    参数:
        store: 内容寻址对象存储。
        content: 待写入的原始字节。

    返回:
        ``sha256:<digest>`` 形式的 restore_ref。

    异常:
        见 ``ChangeSetBlobStore.stage`` / ``publish``。

    副作用:
        在 store 的 staging/blobs 目录落盘。

    说明:
        生产路径通过预写快照保护 ``stage``→``publish`` 两阶段；测试直接串联
        两者取回 ref，不引入额外的便捷写入 API。
    """
    return store.publish(*store.stage(content))


def _save_transition(
    task_id: int,
    run_id: int,
    blob_store: ChangeSetBlobStore,
    *,
    path: str = "note.txt",
    identity: str,
    before: bytes | None,
    after: bytes | None,
) -> None:
    before_ref = _put_blob(blob_store, before) if before is not None else None
    envelope = {
        "operation": {
            "type": "UPDATE",
            "operation_id": f"op-{identity}-{after!r}",
            "tool": "write_file",
        },
        "path_states": [
            {
                "file_identity": identity,
                "path": path,
                "before": _state(before, before_ref),
                "after": _state(after),
            }
        ],
        "display_patch": {"format": "unified", "text": "unused", "truncated": False},
    }
    get_file_snapshot_crud().save_batch_with_sequence(
        task_id,
        [
            FileSnapshotRecord(
                task_id=task_id,
                run_id=run_id,
                tool_call_id=f"call-{identity}-{after!r}",
                path=path,
                op_json=envelope,
            )
        ],
    )


def _service(blob_store: ChangeSetBlobStore) -> TaskChangeSetService:
    return TaskChangeSetService(blob_store=blob_store)


def test_mutation_intents_are_stored_only_in_file_snapshots(change_set_env) -> None:
    with main_session_factory()() as session:
        inspector = inspect(session.get_bind())
        tables = set(inspector.get_table_names())
        snapshot_columns = {column["name"] for column in inspector.get_columns("file_snapshots")}

    assert "file_mutation_journals" not in tables
    assert {"mutation_id", "mutation_state", "mutation_json"} <= snapshot_columns
    assert (
        not {
            "operation_id",
            "tool_name",
            "action",
            "additions",
            "deletions",
            "stable",
            "reverted_at",
        }
        & snapshot_columns
    )


def test_final_net_diff_and_revert_restore_the_oldest_pending_baseline(change_set_env) -> None:
    root, task_id, run_id, blobs = change_set_env
    original = b"before\r\n"
    middle = b"middle\r\n"
    final = b"final\r\n"
    (root / "note.txt").write_bytes(final)
    _save_transition(task_id, run_id, blobs, identity="file-1", before=original, after=middle)
    later_run = get_conversation_run_crud().create(task_id, "second run", status="cancelled")
    _save_transition(task_id, later_run.id, blobs, identity="file-1", before=middle, after=final)
    service = _service(blobs)

    changes = service.get_changes(task_id)
    group = changes["files"][0]
    assert group["operation_count"] == 2
    assert group["net_diff"]["state"] == "verified"
    assert group["net_diff"]["additions"] == 1
    assert group["net_diff"]["deletions"] == 1
    assert group["net_diff"]["has_unrendered_changes"] is False
    assert "before" in group["net_diff"]["patch"]
    assert "final" in group["net_diff"]["patch"]
    assert "middle" not in group["net_diff"]["patch"]

    response = service.apply(task_id, "revert", [group["change_id"]])

    assert response["results"][0]["outcome"] == "reverted"
    assert (root / "note.txt").read_bytes() == original
    assert response["change_set"]["files"] == []
    assert (
        service.apply(task_id, "revert", [group["change_id"]])["results"][0]["outcome"]
        == "already_reverted"
    )

    (root / "note.txt").write_bytes(b"new run state\r\n")
    _save_transition(
        task_id,
        later_run.id,
        blobs,
        identity="file-1",
        before=original,
        after=b"new run state\r\n",
    )
    new_group = service.get_changes(task_id)["files"][0]
    assert new_group["change_id"] != group["change_id"]
    stale_retry = service.apply(task_id, "revert", [group["change_id"]])
    assert stale_retry["results"][0]["outcome"] == "already_reverted"
    assert (root / "note.txt").read_bytes() == b"new run state\r\n"


def test_terminal_run_status_is_the_only_snapshot_lifecycle_fact(
    change_set_env, monkeypatch
) -> None:
    _, task_id, _, blobs = change_set_env
    run_crud = get_conversation_run_crud()
    state_service = ConversationRunStateService()
    monkeypatch.setattr(
        "app.service.task.conversation_run_state_service.dispatch_conversation_event",
        lambda _event: None,
    )
    transitions = (
        ("completed", state_service.complete_run_if_running),
        ("failed", state_service.fail_run_if_running),
        ("cancelled", state_service.cancel_run_if_running),
    )

    for status, transition in transitions:
        run = run_crud.create(task_id, status, status="running")
        _save_transition(
            task_id,
            run.id,
            blobs,
            path=f"{status}.txt",
            identity=status,
            before=b"before",
            after=b"after",
        )
        assert transition(run.id) is not None
        snapshots = get_file_snapshot_crud().list_by_turn(run.id)
        assert len(snapshots) == 1
        assert run_crud.get(run.id).status == status


def test_startup_reconciles_snapshots_for_orphaned_cancelled_runs(change_set_env) -> None:
    _, task_id, _, blobs = change_set_env
    run = get_conversation_run_crud().create(task_id, "crash recovery", status="running")
    _save_transition(
        task_id,
        run.id,
        blobs,
        path="recovered.txt",
        identity="recovered-run",
        before=b"before",
        after=b"after",
    )

    recovered = ConversationRunService().recover_orphaned_runs()

    assert [item.id for item in recovered] == [run.id]
    assert get_conversation_run_crud().get(run.id).status == "cancelled"
    snapshots = get_file_snapshot_crud().list_by_turn(run.id)
    assert len(snapshots) == 1


def test_external_edit_conflicts_without_touching_file_or_snapshot_status(change_set_env) -> None:
    root, task_id, run_id, blobs = change_set_env
    original, agent, human = b"base\n", b"agent\n", b"human\n"
    (root / "note.txt").write_bytes(human)
    _save_transition(task_id, run_id, blobs, identity="file-2", before=original, after=agent)
    service = _service(blobs)

    group = service.get_changes(task_id)["files"][0]
    assert group["net_diff"]["state"] == "conflict"
    result = service.apply(task_id, "revert", [group["change_id"]])["results"][0]

    assert result["outcome"] == "conflict"
    assert (root / "note.txt").read_bytes() == human
    assert get_file_snapshot_crud().list_any_by_task(task_id)[0].status == "pending"


def test_broken_middle_chain_conflicts_even_when_the_final_state_matches_baseline(
    change_set_env,
) -> None:
    root, task_id, run_id, blobs = change_set_env
    baseline = b"baseline\n"
    (root / "note.txt").write_bytes(baseline)
    _save_transition(
        task_id,
        run_id,
        blobs,
        identity="broken-chain",
        before=baseline,
        after=b"first agent state\n",
    )
    _save_transition(
        task_id,
        run_id,
        blobs,
        identity="broken-chain",
        before=b"unrecorded middle state\n",
        after=baseline,
    )
    service = _service(blobs)

    group = service.get_changes(task_id)["files"][0]
    result = service.apply(task_id, "revert", [group["change_id"]])["results"][0]

    assert group["net_diff"]["state"] == "conflict"
    assert result["outcome"] == "conflict"
    assert (root / "note.txt").read_bytes() == baseline
    assert all(
        snapshot.status == "pending"
        for snapshot in get_file_snapshot_crud().list_any_by_task(task_id)
    )


def test_batch_revert_keeps_success_when_another_file_conflicts(change_set_env) -> None:
    root, task_id, run_id, blobs = change_set_env
    (root / "safe.txt").write_bytes(b"agent safe\n")
    (root / "conflict.txt").write_bytes(b"human edit\n")
    _save_transition(
        task_id,
        run_id,
        blobs,
        path="safe.txt",
        identity="safe-file",
        before=b"baseline safe\n",
        after=b"agent safe\n",
    )
    _save_transition(
        task_id,
        run_id,
        blobs,
        path="conflict.txt",
        identity="conflict-file",
        before=b"baseline conflict\n",
        after=b"agent conflict\n",
    )
    service = _service(blobs)
    files = service.get_changes(task_id)["files"]
    ids_by_path = {item["paths"][0]: item["change_id"] for item in files}

    response = service.apply(
        task_id,
        "revert",
        [ids_by_path["safe.txt"], ids_by_path["conflict.txt"]],
    )

    outcomes = {item["change_id"]: item["outcome"] for item in response["results"]}
    assert outcomes[ids_by_path["safe.txt"]] == "reverted"
    assert outcomes[ids_by_path["conflict.txt"]] == "conflict"
    assert (root / "safe.txt").read_bytes() == b"baseline safe\n"
    assert (root / "conflict.txt").read_bytes() == b"human edit\n"
    status_by_path = {
        snapshot.path: snapshot.status
        for snapshot in get_file_snapshot_crud().list_any_by_task(task_id)
    }
    assert status_by_path == {"safe.txt": "reverted", "conflict.txt": "pending"}


@pytest.mark.parametrize(
    ("path", "before", "after", "live"),
    [
        ("new.txt", None, b"agent content", b"human replacement"),
        ("deleted.txt", b"original", None, b"human recreation"),
    ],
)
def test_add_or_delete_target_changed_by_human_is_not_overwritten(
    change_set_env,
    path,
    before,
    after,
    live,
) -> None:
    root, task_id, run_id, blobs = change_set_env
    (root / path).write_bytes(live)
    _save_transition(
        task_id,
        run_id,
        blobs,
        path=path,
        identity=f"manual-{path}",
        before=before,
        after=after,
    )
    service = _service(blobs)
    group = service.get_changes(task_id)["files"][0]

    result = service.apply(task_id, "revert", [group["change_id"]])["results"][0]

    assert result["outcome"] == "conflict"
    assert (root / path).read_bytes() == live
    assert get_file_snapshot_crud().list_any_by_task(task_id)[0].status == "pending"


def test_move_target_modified_by_human_is_not_overwritten(change_set_env) -> None:
    root, task_id, run_id, blobs = change_set_env
    source = "old.txt"
    destination = "new.txt"
    agent_content = b"moved agent file"
    human_content = b"human edit after move"
    (root / destination).write_bytes(human_content)
    before_ref = _put_blob(blobs, agent_content)
    envelope = {
        "operation": {"type": "MOVE", "operation_id": "move-human-edit"},
        "path_states": [
            {
                "file_identity": "moved-human-edit",
                "path": source,
                "before": _state(agent_content, before_ref),
                "after": {"exists": False},
            },
            {
                "file_identity": "moved-human-edit",
                "path": destination,
                "before": {"exists": False},
                "after": _state(agent_content),
            },
        ],
    }
    get_file_snapshot_crud().save_batch_with_sequence(
        task_id,
        [
            FileSnapshotRecord(
                task_id=task_id,
                run_id=run_id,
                tool_call_id="move-human-edit",
                path=source,
                op_json=envelope,
            )
        ],
    )
    service = _service(blobs)
    group = service.get_changes(task_id)["files"][0]

    result = service.apply(task_id, "revert", [group["change_id"]])["results"][0]

    assert result["outcome"] == "conflict"
    assert not (root / source).exists()
    assert (root / destination).read_bytes() == human_content


def test_add_update_delete_projects_zero_net_diff_and_reverts_as_a_noop(change_set_env) -> None:
    root, task_id, run_id, blobs = change_set_env
    first, second = b"one\n", b"two\n"
    _save_transition(task_id, run_id, blobs, identity="new-file", before=None, after=first)
    _save_transition(task_id, run_id, blobs, identity="new-file", before=first, after=second)
    _save_transition(task_id, run_id, blobs, identity="new-file", before=second, after=None)
    service = _service(blobs)

    group = service.get_changes(task_id)["files"][0]
    assert not (root / "note.txt").exists()
    assert group["operation_count"] == 3
    assert group["action"] == "unchanged"
    assert group["net_diff"] == {
        "state": "verified",
        "additions": 0,
        "deletions": 0,
        "patch": None,
        "truncated": False,
        "has_unrendered_changes": False,
    }
    result = service.apply(task_id, "revert", [group["change_id"]])["results"][0]

    assert result["outcome"] == "reverted"
    assert not (root / "note.txt").exists()
    assert all(
        row.status == "reverted" for row in get_file_snapshot_crud().list_any_by_task(task_id)
    )


def test_empty_file_creation_is_not_misreported_as_zero_net_change(change_set_env) -> None:
    root, task_id, run_id, blobs = change_set_env
    (root / "note.txt").write_bytes(b"")
    _save_transition(task_id, run_id, blobs, identity="empty-file", before=None, after=b"")

    group = _service(blobs).get_changes(task_id)["files"][0]

    assert group["action"] == "added"
    assert group["net_diff"]["patch"] is None
    assert group["net_diff"]["additions"] == 0
    assert group["net_diff"]["deletions"] == 0
    assert group["net_diff"]["has_unrendered_changes"] is True


def test_legacy_snapshot_without_exact_state_material_fails_closed(change_set_env) -> None:
    _, task_id, run_id, blobs = change_set_env
    get_file_snapshot_crud().save_batch_with_sequence(
        task_id,
        [
            FileSnapshotRecord(
                task_id=task_id,
                run_id=run_id,
                tool_call_id="legacy-snapshot",
                path="legacy.txt",
            )
        ],
    )
    service = _service(blobs)

    group = service.get_changes(task_id)["files"][0]
    result = service.apply(task_id, "revert", [group["change_id"]])["results"][0]

    assert group["net_diff"]["state"] == "unverifiable"
    assert result["outcome"] == "snapshot_unverifiable"
    assert get_file_snapshot_crud().list_any_by_task(task_id)[0].status == "pending"


def test_binary_file_change_is_reported_without_fabricating_a_text_patch(change_set_env) -> None:
    root, task_id, run_id, blobs = change_set_env
    before, after = b"\x00before", b"\x00after"
    (root / "note.txt").write_bytes(after)
    _save_transition(task_id, run_id, blobs, identity="binary-file", before=before, after=after)

    diff = _service(blobs).get_changes(task_id)["files"][0]["net_diff"]

    assert diff["state"] == "verified"
    assert diff["patch"] is None
    assert diff["has_unrendered_changes"] is True


def test_pure_move_is_not_misreported_as_zero_net_change(change_set_env) -> None:
    root, task_id, run_id, blobs = change_set_env
    content = b"same content\n"
    (root / "new.txt").write_bytes(content)
    before_state = _state(content, _put_blob(blobs, content))
    after_state = _state(content)
    envelope = {
        "operation": {"type": "MOVE", "operation_id": "move-1", "tool": "apply_patch"},
        "path_states": [
            {
                "file_identity": "moved-file",
                "path": "old.txt",
                "before": before_state,
                "after": {"exists": False},
            },
            {
                "file_identity": "moved-file",
                "path": "new.txt",
                "before": {"exists": False},
                "after": after_state,
            },
        ],
    }
    get_file_snapshot_crud().save_batch_with_sequence(
        task_id,
        [
            FileSnapshotRecord(
                task_id=task_id,
                run_id=run_id,
                tool_call_id="move-call",
                path="old.txt",
                op_json=envelope,
            )
        ],
    )

    group = _service(blobs).get_changes(task_id)["files"][0]

    assert group["action"] == "moved"
    assert group["paths"] == ["new.txt", "old.txt"]
    assert group["net_diff"]["patch"] is None
    assert group["net_diff"]["has_unrendered_changes"] is True


def test_keep_starts_a_new_baseline_for_later_revert(change_set_env) -> None:
    root, task_id, run_id, blobs = change_set_env
    original, kept, later = b"zero\n", b"one\n", b"two\n"
    (root / "note.txt").write_bytes(kept)
    _save_transition(task_id, run_id, blobs, identity="file-3", before=original, after=kept)
    service = _service(blobs)

    first_group = service.get_changes(task_id)["files"][0]
    assert (
        service.apply(task_id, "keep", [first_group["change_id"]])["results"][0]["outcome"]
        == "kept"
    )
    (root / "note.txt").write_bytes(later)
    _save_transition(task_id, run_id, blobs, identity="file-3", before=kept, after=later)
    next_group = service.get_changes(task_id)["files"][0]

    assert next_group["operation_count"] == 1
    assert (
        service.apply(task_id, "revert", [next_group["change_id"]])["results"][0]["outcome"]
        == "reverted"
    )
    assert (root / "note.txt").read_bytes() == kept
    first_snapshot = get_file_snapshot_crud().list_any_by_task(task_id)[0]
    first_envelope = first_snapshot.op_json
    assert "restore_ref" not in first_envelope["path_states"][0]["before"]


def test_blob_gc_keeps_pending_and_prepared_snapshot_refs_and_ignores_staging(
    change_set_env,
) -> None:
    root, task_id, run_id, blobs = change_set_env
    baseline = b"shared before image\n"
    (root / "note.txt").write_bytes(b"kept result\n")
    (root / "other.txt").write_bytes(b"pending result\n")
    _save_transition(
        task_id,
        run_id,
        blobs,
        identity="kept-gc",
        before=baseline,
        after=b"kept result\n",
    )
    _save_transition(
        task_id,
        run_id,
        blobs,
        path="other.txt",
        identity="pending-gc",
        before=baseline,
        after=b"pending result\n",
    )
    changes = _service(blobs).get_changes(task_id)["files"]
    kept_group = next(item for item in changes if item["paths"] == ["note.txt"])
    kept = _service(blobs).apply(task_id, "keep", [kept_group["change_id"]])
    assert kept["results"][0]["outcome"] == "kept"

    shared_ref = _put_blob(blobs, b"active prepared image")
    unused_ref = _put_blob(blobs, b"orphan image")
    staging_id, _ = blobs.stage(b"unpublished image")
    get_file_snapshot_crud().save_batch_with_sequence(
        task_id,
        [
            FileSnapshotRecord(
                task_id=task_id,
                run_id=run_id,
                tool_call_id="gc-call",
                mutation_id="gc-op",
                mutation_state="prepared",
                mutation_json={
                    "kind": "agent_mutation",
                    "workspace_id": 1,
                    "snapshot_paths": ["cache.bin"],
                    "implicit_directory_paths": [],
                    "move_pairs": [],
                    "staging": [{"id": staging_id, "sha256": "unused"}],
                },
                path="cache.bin",
                op_json={
                    "operation": {"type": "UPDATE", "operation_id": "gc-op"},
                    "path_states": [
                        {
                            "file_identity": "cache-file",
                            "path": "cache.bin",
                            "before": {
                                "exists": True,
                                "entry_type": "file",
                                "restore_ref": shared_ref,
                            },
                            "after": {"exists": True, "entry_type": "file"},
                        }
                    ],
                },
            )
        ],
    )
    gc = ChangeSetBlobGarbageCollector(blobs, get_file_snapshot_crud())

    assert gc.collect() == 1
    with pytest.raises(FileNotFoundError):
        blobs.read(unused_ref)
    assert blobs.read(shared_ref) == b"active prepared image"
    pending_snapshot = next(
        snapshot
        for snapshot in get_file_snapshot_crud().list_any_by_task(task_id)
        if snapshot.path == "other.txt"
    )
    pending_envelope = pending_snapshot.op_json
    pending_ref = pending_envelope["path_states"][0]["before"]["restore_ref"]
    assert blobs.read(pending_ref) == baseline
    assert (blobs.staging_dir / staging_id).exists()


def test_blob_gc_serializes_prepared_snapshot_publication_and_never_deletes_staging(
    change_set_env,
    monkeypatch,
) -> None:
    _root, _task_id, _run_id, blobs = change_set_env
    publisher_task = get_task_crud().create(1, "concurrent blob publisher")
    publisher_run = get_conversation_run_crud().create(
        publisher_task.id,
        "concurrent blob publisher run",
        status="cancelled",
    )
    content = b"concurrent before image"
    restore_ref = _put_blob(blobs, content)
    staging_id, digest = blobs.stage(content)
    entered_collection = threading.Event()
    release_collection = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()
    gc_results: list[int] = []
    snapshots: list[FileSnapshotRecord] = []
    failures: list[BaseException] = []
    original_collect = blobs.collect_unreferenced

    def pause_collection(references) -> int:
        entered_collection.set()
        assert release_collection.wait(timeout=5)
        return original_collect(references)

    monkeypatch.setattr(blobs, "collect_unreferenced", pause_collection)
    snapshot_crud = get_file_snapshot_crud()
    gc = ChangeSetBlobGarbageCollector(blobs, snapshot_crud)

    def collect() -> None:
        try:
            gc_results.append(gc.collect())
        except BaseException as exc:
            failures.append(exc)

    def publish_prepared_snapshot_reference() -> None:
        writer_started.set()
        try:
            record = FileSnapshotRecord(
                task_id=publisher_task.id,
                run_id=publisher_run.id,
                tool_call_id="gc-concurrent-call",
                mutation_id="gc-concurrent-operation",
                mutation_state="prepared",
                mutation_json={
                    "kind": "agent_mutation",
                    "workspace_id": 1,
                    "snapshot_paths": ["file.bin"],
                    "implicit_directory_paths": [],
                    "move_pairs": [],
                    "staging": [{"id": staging_id, "sha256": digest}],
                },
                path="file.bin",
                op_json={
                    "operation": {
                        "type": "UPDATE",
                        "operation_id": "gc-concurrent-operation",
                    },
                    "path_states": [
                        {
                            "file_identity": "concurrent-file",
                            "path": "file.bin",
                            "before": {
                                "exists": True,
                                "entry_type": "file",
                                "restore_ref": restore_ref,
                            },
                            "after": {"exists": True, "entry_type": "file"},
                        }
                    ],
                },
            )
            snapshots.extend(snapshot_crud.save_batch_with_sequence(publisher_task.id, [record]))
        except BaseException as exc:
            failures.append(exc)
        finally:
            writer_done.set()

    collector = threading.Thread(target=collect)
    writer = threading.Thread(target=publish_prepared_snapshot_reference)
    collector.start()
    assert entered_collection.wait(timeout=3)
    assert (blobs.staging_dir / staging_id).exists()
    writer.start()
    assert writer_started.wait(timeout=3)
    try:
        assert not writer_done.wait(timeout=0.05)
    finally:
        release_collection.set()
    collector.join(timeout=5)
    writer.join(timeout=5)

    assert not collector.is_alive()
    assert not writer.is_alive()
    assert failures == []
    assert gc_results == [1]
    assert len(snapshots) == 1
    with pytest.raises(FileNotFoundError):
        blobs.read(restore_ref)
    assert (blobs.staging_dir / staging_id).exists()
    assert blobs.publish(staging_id, digest) == restore_ref
    assert blobs.read(restore_ref) == content
    monkeypatch.setattr(blobs, "collect_unreferenced", original_collect)
    assert gc.collect() == 0


@pytest.mark.parametrize(
    "failure_point",
    ["before_image_staging", "partial_before_image", "prepared_snapshot_sqlite"],
)
def test_change_set_storage_failure_prevents_first_workspace_write(
    change_set_env,
    monkeypatch,
    failure_point: str,
) -> None:
    root, task_id, run_id, blobs = change_set_env
    target = root / "note.txt"
    target.write_bytes(b"baseline")
    service = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=run_id,
    )
    handler_called = False

    if failure_point == "before_image_staging":

        def fail_staging(_content: bytes) -> tuple[str, str]:
            raise OSError("injected before-image storage failure")

        monkeypatch.setattr(blobs, "stage", fail_staging)
    elif failure_point == "partial_before_image":

        def fail_fsync(_fd: int) -> None:
            raise OSError("injected before-image fsync failure")

        monkeypatch.setattr(blob_store_module.os, "fsync", fail_fsync)
    else:

        def fail_snapshot(*_args, **_kwargs):
            raise RuntimeError("injected SQLite prepared snapshot failure")

        monkeypatch.setattr(service._snapshots, "save_batch_with_sequence", fail_snapshot)

    def handler() -> ToolObservation:
        nonlocal handler_called
        handler_called = True
        atomic_write_text(target, "agent change", preserve_eol=False)
        return ToolObservation(tool_name="write_file", status="success")

    observation = service.execute_tool_mutation(
        tool_name="write_file",
        arguments={"path": "note.txt"},
        execution_context=context,
        tool_call_id=f"storage-failure-{failure_point}",
        write_paths=[target],
        execute=handler,
    )

    assert observation.status == "error"
    assert not handler_called
    assert target.read_bytes() == b"baseline"
    assert get_file_snapshot_crud().list_any_by_task(task_id) == []
    assert list(blobs.staging_dir.iterdir()) == []
    assert list(blobs.blobs_dir.iterdir()) == []


@pytest.mark.parametrize("delete_fails", [False, True])
def test_publish_failure_does_not_leave_an_unrecoverable_prewrite(
    change_set_env,
    monkeypatch,
    delete_fails: bool,
) -> None:
    root, task_id, run_id, blobs = change_set_env
    target = root / "note.txt"
    target.write_bytes(b"baseline")
    service = FileMutationService(snapshot_crud=get_file_snapshot_crud(), blob_store=blobs)
    context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=run_id,
    )
    original_publish = blobs.publish
    original_delete_prepared = service._snapshots.delete_prepared
    handler_called = False

    def fail_publish(_staging_id: str, _digest: str) -> str:
        raise OSError("injected blob publish failure")

    monkeypatch.setattr(blobs, "publish", fail_publish)
    if delete_fails:

        def fail_prepared_cleanup(_operation_id: str) -> int:
            raise OSError("injected prepared snapshot cleanup failure")

        monkeypatch.setattr(service._snapshots, "delete_prepared", fail_prepared_cleanup)

    def handler() -> ToolObservation:
        nonlocal handler_called
        handler_called = True
        target.write_bytes(b"changed")
        return ToolObservation(tool_name="write_file", status="success")

    observation = service.execute_tool_mutation(
        tool_name="write_file",
        arguments={"path": "note.txt"},
        execution_context=context,
        tool_call_id="publish-failure",
        write_paths=[target],
        execute=handler,
    )

    assert observation.status == "error"
    assert not handler_called
    assert target.read_bytes() == b"baseline"
    prepared = get_file_snapshot_crud().list_unfinalized()
    assert _service(blobs).get_changes(task_id)["files"] == []

    if not delete_fails:
        assert prepared == []
        assert get_file_snapshot_crud().list_any_by_task(task_id) == []
        assert list(blobs.staging_dir.iterdir()) == []
        assert list(blobs.blobs_dir.iterdir()) == []
        return

    assert len(prepared) == 1
    manifest = prepared[0].mutation_json
    assert manifest is not None
    assert "schema_version" not in manifest
    assert "schema_version" not in prepared[0].op_json
    staging_id = manifest["staging"][0]["id"]
    assert (blobs.staging_dir / staging_id).is_file()
    monkeypatch.setattr(blobs, "publish", original_publish)
    monkeypatch.setattr(service._snapshots, "delete_prepared", original_delete_prepared)
    service._workspace_root = lambda _workspace_id: root
    assert service.reconcile_prepared_snapshots() == []
    assert get_file_snapshot_crud().list_unfinalized() == []
    assert get_file_snapshot_crud().list_any_by_task(task_id) == []
    assert list(blobs.staging_dir.iterdir()) == []
    assert target.read_bytes() == b"baseline"


def test_prepared_snapshot_stays_hidden_while_file_handler_is_running(change_set_env) -> None:
    root, task_id, run_id, blobs = change_set_env
    target = root / "note.txt"
    target.write_bytes(b"baseline")
    service = FileMutationService(snapshot_crud=get_file_snapshot_crud(), blob_store=blobs)
    context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=run_id,
    )
    handler_started = threading.Event()
    allow_write = threading.Event()
    observations: list[ToolObservation] = []
    failures: list[BaseException] = []

    def handler() -> ToolObservation:
        handler_started.set()
        if not allow_write.wait(timeout=5):
            raise TimeoutError("test did not release file handler")
        target.write_bytes(b"changed")
        return ToolObservation(tool_name="write_file", status="success")

    def mutate() -> None:
        try:
            observations.append(
                service.execute_tool_mutation(
                    tool_name="write_file",
                    arguments={"path": "note.txt"},
                    execution_context=context,
                    tool_call_id="hidden-while-running",
                    write_paths=[target],
                    execute=handler,
                )
            )
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=mutate)
    worker.start()
    try:
        assert handler_started.wait(timeout=5)
        prepared = get_file_snapshot_crud().list_unfinalized()
        assert len(prepared) == 1
        assert _service(blobs).get_changes(task_id)["files"] == []
    finally:
        allow_write.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert failures == []
    assert observations[0].status == "success"
    assert _service(blobs).get_changes(task_id)["files"][0]["paths"] == ["note.txt"]


def test_later_before_image_failure_discards_already_staged_objects(
    change_set_env,
    monkeypatch,
) -> None:
    _root, _task_id, _run_id, blobs = change_set_env
    service = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    original_stage = blobs.stage
    calls = 0

    def fail_second_stage(content: bytes) -> tuple[str, str]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second before-image failure")
        return original_stage(content)

    monkeypatch.setattr(blobs, "stage", fail_second_stage)
    captures = [
        CapturedPath(
            path="one.txt",
            state={"exists": True, "entry_type": "file"},
            content=b"first image",
        ),
        CapturedPath(
            path="two.txt",
            state={"exists": True, "entry_type": "file"},
            content=b"second image",
        ),
    ]

    with pytest.raises(OSError, match="injected second before-image failure"):
        service._stage_before_images(captures, tool_name="write_file", operation_id="mut_test")

    assert calls == 2
    assert list(blobs.staging_dir.iterdir()) == []
    assert list(blobs.blobs_dir.iterdir()) == []


def test_interrupted_revert_reconciliation_keeps_the_observed_workspace_state(
    change_set_env,
) -> None:
    root, task_id, run_id, blobs = change_set_env
    path = root / "note.txt"
    path.write_bytes(b"agent result")
    _save_transition(
        task_id,
        run_id,
        blobs,
        identity="interrupted-revert",
        before=b"baseline",
        after=b"agent result",
    )
    snapshot = get_file_snapshot_crud().list_any_by_task(task_id)[0]
    mutation_id = "revert-interrupted"
    with main_session_factory().begin() as session:
        assert (
            get_file_snapshot_crud().begin_revert([snapshot.id], mutation_id, session=session) == 1
        )
    path.write_bytes(b"baseline")

    assert _service(blobs).reconcile_interrupted_reverts() == []
    reconciled = get_file_snapshot_crud().list_any_by_task(task_id)[0]
    assert path.read_bytes() == b"baseline"
    assert reconciled.status == "reverted"
    assert reconciled.mutation_state == "applied"
    assert _service(blobs).get_changes(task_id)["files"] == []


@pytest.mark.parametrize(
    "disk_content,expected_status",
    [(b"baseline", "reverted"), (b"final", "pending")],
)
def test_interrupted_revert_preserves_same_file_history_chain(
    change_set_env,
    disk_content: bytes,
    expected_status: str,
) -> None:
    root, task_id, run_id, blobs = change_set_env
    path = root / "note.txt"
    path.write_bytes(b"final")
    _save_transition(
        task_id,
        run_id,
        blobs,
        identity="same-file-chain",
        before=b"baseline",
        after=b"middle",
    )
    _save_transition(
        task_id,
        run_id,
        blobs,
        identity="same-file-chain",
        before=b"middle",
        after=b"final",
    )
    snapshots = get_file_snapshot_crud().list_any_by_task(task_id)
    mutation_id = "revert-same-file-chain"
    with main_session_factory().begin() as session:
        assert get_file_snapshot_crud().begin_revert(
            [snapshot.id for snapshot in snapshots], mutation_id, session=session
        ) == len(snapshots)
    path.write_bytes(disk_content)

    assert _service(blobs).reconcile_interrupted_reverts() == []
    reconciled = get_file_snapshot_crud().list_any_by_task(task_id)
    assert [snapshot.status for snapshot in reconciled] == [expected_status, expected_status]
    if disk_content == b"baseline":
        assert _service(blobs).get_changes(task_id)["files"] == []
        return

    history = [snapshot.op_json["path_states"][0] for snapshot in reconciled]
    assert [entry["after"]["sha256"] for entry in history] == [
        hashlib.sha256(b"middle").hexdigest(),
        hashlib.sha256(b"final").hexdigest(),
    ]
    group = _service(blobs).get_changes(task_id)["files"][0]
    result = _service(blobs).apply(task_id, "revert", [group["change_id"]])
    assert result["results"][0]["outcome"] == "reverted"
    assert path.read_bytes() == b"baseline"


def test_agent_write_crash_records_the_current_manual_edit_without_restoring_files(
    change_set_env,
) -> None:
    root, task_id, run_id, blobs = change_set_env
    path = root / "note.txt"
    baseline = b"before\r\n"
    path.write_bytes(baseline)
    service = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=run_id,
    )

    class SimulatedCrash(BaseException):
        pass

    def write_then_crash():
        atomic_write_text(path, "agent result\n", containment_root=root)
        raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        service.execute_tool_mutation(
            tool_name="replace",
            arguments={},
            execution_context=context,
            tool_call_id="write-crash",
            write_paths=[path],
            execute=write_then_crash,
        )

    path.write_bytes(b"human edit")
    restarted = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    restarted._workspace_root = lambda _workspace_id: root
    assert restarted.reconcile_prepared_snapshots() == []
    assert path.read_bytes() == b"human edit"
    assert _service(blobs).get_changes(task_id)["files"][0]["paths"] == ["note.txt"]


def test_crash_after_atomic_write_records_the_landed_file(change_set_env) -> None:
    root, task_id, run_id, blobs = change_set_env
    path = root / "note.txt"
    baseline = b"before\r\n"
    path.write_bytes(baseline)
    service = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=run_id,
    )

    class SimulatedCrash(BaseException):
        pass

    def write_then_crash():
        atomic_write_text(path, "agent result\n", containment_root=root)
        raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        service.execute_tool_mutation(
            tool_name="write_file",
            arguments={},
            execution_context=context,
            tool_call_id="write-crash-recover",
            write_paths=[path],
            execute=write_then_crash,
        )

    assert get_file_snapshot_crud().list_any_by_task(task_id) == []
    assert _service(blobs).get_changes(task_id)["files"] == []

    restarted = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    restarted._workspace_root = lambda _workspace_id: root
    assert restarted.reconcile_prepared_snapshots() == []
    assert path.read_bytes() == b"agent result\r\n"
    assert _service(blobs).get_changes(task_id)["files"][0]["paths"] == ["note.txt"]


def test_interrupted_file_tool_that_did_not_change_disk_drops_its_prewrite_snapshot(
    change_set_env,
) -> None:
    root, task_id, run_id, blobs = change_set_env
    path = root / "note.txt"
    path.write_bytes(b"unchanged")
    service = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=run_id,
    )

    class SimulatedCrash(BaseException):
        pass

    def crash_before_write():
        raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        service.execute_tool_mutation(
            tool_name="write_file",
            arguments={"path": "note.txt"},
            execution_context=context,
            tool_call_id="crash-before-write",
            write_paths=[path],
            execute=crash_before_write,
        )

    assert _service(blobs).get_changes(task_id)["files"] == []
    restarted = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    restarted._workspace_root = lambda _workspace_id: root
    assert restarted.reconcile_prepared_snapshots() == []
    assert get_file_snapshot_crud().list_any_by_task(task_id) == []
    assert path.read_bytes() == b"unchanged"


def test_nested_file_revert_removes_only_agent_created_empty_parent_directories(
    change_set_env,
) -> None:
    root, task_id, run_id, blobs = change_set_env
    path = root / "new-parent" / "nested" / "note.txt"
    service = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=run_id,
    )
    observation = ToolObservation(tool_name="write_file", status="success", content="written")

    def write_file():
        atomic_write_text(path, "written", containment_root=root)
        return observation

    result = service.execute_tool_mutation(
        tool_name="write_file",
        arguments={},
        execution_context=context,
        tool_call_id="nested-write",
        write_paths=[path],
        execute=write_file,
    )

    assert result.status == "success"
    assert path.read_text(encoding="utf-8") == "written"
    change = _service(blobs).get_changes(task_id)["files"][0]
    assert change["paths"] == ["new-parent/nested/note.txt"]
    assert change["net_diff"]["has_unrendered_changes"] is True

    reverted = _service(blobs).apply(task_id, "revert", [change["change_id"]])

    assert reverted["results"][0]["outcome"] == "reverted"
    assert not path.exists()
    assert not (root / "new-parent").exists()


@pytest.mark.parametrize("external_change", ["replace_directory", "add_sibling"])
def test_nested_file_revert_preserves_external_parent_directory_changes(
    change_set_env,
    external_change,
) -> None:
    root, task_id, run_id, blobs = change_set_env
    parent = root / "created" / "nested"
    path = parent / "note.txt"
    service = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=run_id,
    )

    def write_file():
        atomic_write_text(path, "agent content", containment_root=root)
        return ToolObservation(tool_name="write_file", status="success", content="written")

    service.execute_tool_mutation(
        tool_name="write_file",
        arguments={},
        execution_context=context,
        tool_call_id="nested-write-external-change",
        write_paths=[path],
        execute=write_file,
    )
    change = _service(blobs).get_changes(task_id)["files"][0]

    if external_change == "replace_directory":
        content = path.read_bytes()
        shutil.rmtree(root / "created")
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
    else:
        (parent / "human.txt").write_text("human content", encoding="utf-8")

    result = _service(blobs).apply(task_id, "revert", [change["change_id"]])

    expected_outcome = "conflict" if external_change == "replace_directory" else "reverted"
    assert result["results"][0]["outcome"] == expected_outcome
    if external_change == "replace_directory":
        assert path.read_text(encoding="utf-8") == "agent content"
    else:
        assert not path.exists()
    if external_change == "add_sibling":
        assert (parent / "human.txt").read_text(encoding="utf-8") == "human content"
        assert parent.is_dir()
    expected_status = "pending" if external_change == "replace_directory" else "reverted"
    assert get_file_snapshot_crud().list_any_by_task(task_id)[0].status == expected_status


def test_batch_revert_of_sibling_new_files_removes_the_shared_empty_parent_once(
    change_set_env,
) -> None:
    root, task_id, run_id, blobs = change_set_env
    first = root / "new-parent" / "first.txt"
    second = root / "new-parent" / "second.txt"
    service = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=run_id,
    )

    def write_files():
        atomic_write_text(first, "first", containment_root=root)
        atomic_write_text(second, "second", containment_root=root)
        return ToolObservation(tool_name="apply_patch", status="success", content="written")

    service.execute_tool_mutation(
        tool_name="apply_patch",
        arguments={},
        execution_context=context,
        tool_call_id="sibling-new-files",
        write_paths=[first, second],
        execute=write_files,
    )
    changes = _service(blobs).get_changes(task_id)["files"]
    assert len(changes) == 2

    response = _service(blobs).apply(
        task_id,
        "revert",
        [change["change_id"] for change in changes],
    )

    assert [item["outcome"] for item in response["results"]] == ["reverted", "reverted"]
    assert not (root / "new-parent").exists()
    assert not first.exists()
    assert not second.exists()


def test_single_file_group_reverts_while_sibling_file_keeps_the_parent_directory(
    change_set_env,
) -> None:
    root, task_id, run_id, blobs = change_set_env
    first = root / "new-parent" / "first.txt"
    second = root / "new-parent" / "second.txt"
    service = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=run_id,
    )

    def write_files():
        atomic_write_text(first, "first", containment_root=root)
        atomic_write_text(second, "second", containment_root=root)
        return ToolObservation(tool_name="apply_patch", status="success", content="written")

    service.execute_tool_mutation(
        tool_name="apply_patch",
        arguments={},
        execution_context=context,
        tool_call_id="single-sibling-revert",
        write_paths=[first, second],
        execute=write_files,
    )
    change_service = _service(blobs)
    changes = change_service.get_changes(task_id)["files"]
    first_change = next(group for group in changes if group["paths"] == ["new-parent/first.txt"])
    second_change = next(group for group in changes if group["paths"] == ["new-parent/second.txt"])

    first_result = change_service.apply(task_id, "revert", [first_change["change_id"]])

    assert first_result["results"][0]["outcome"] == "reverted"
    assert not first.exists()
    assert second.read_text(encoding="utf-8") == "second"
    assert (root / "new-parent").is_dir()

    second_result = change_service.apply(task_id, "revert", [second_change["change_id"]])

    assert second_result["results"][0]["outcome"] == "reverted"
    assert not (root / "new-parent").exists()


def test_revert_rechecks_created_directory_identity_after_preparing_snapshot(
    change_set_env,
    monkeypatch,
) -> None:
    root, task_id, run_id, blobs = change_set_env
    parent = root / "created" / "nested"
    path = parent / "note.txt"
    service = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=run_id,
    )

    def write_file():
        atomic_write_text(path, "agent content", containment_root=root)
        return ToolObservation(tool_name="write_file", status="success", content="written")

    service.execute_tool_mutation(
        tool_name="write_file",
        arguments={},
        execution_context=context,
        tool_call_id="directory-race",
        write_paths=[path],
        execute=write_file,
    )
    change = _service(blobs).get_changes(task_id)["files"][0]
    change_service = _service(blobs)
    restore_states = change_service._restore_states

    def replace_directory_before_restore(root_arg, states, **kwargs):
        content = path.read_bytes()
        shutil.rmtree(root_arg / "created")
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
        return restore_states(root_arg, states, **kwargs)

    monkeypatch.setattr(change_service, "_restore_states", replace_directory_before_restore)

    result = change_service.apply(task_id, "revert", [change["change_id"]])

    assert result["results"][0]["outcome"] == "conflict"
    assert path.read_text(encoding="utf-8") == "agent content"


def test_move_then_update_projects_and_reverts_the_original_file_identity(change_set_env) -> None:
    root, task_id, run_id, blobs = change_set_env
    original, final = b"before move\n", b"after move\n"
    (root / "new.ts").write_bytes(final)
    original_ref = _put_blob(blobs, original)
    records = [
        FileSnapshotRecord(
            task_id=task_id,
            run_id=run_id,
            tool_call_id="move-then-update",
            path="old.ts",
            op_json={
                "operation": {
                    "type": "MOVE",
                    "operation_id": "move-identity",
                    "tool": "apply_patch",
                },
                "path_states": [
                    {
                        "file_identity": "moved-file",
                        "path": "old.ts",
                        "before": _state(original, original_ref),
                        "after": {"exists": False},
                    },
                    {
                        "file_identity": "moved-file",
                        "path": "new.ts",
                        "before": {"exists": False},
                        "after": _state(original),
                    },
                ],
            },
        ),
        FileSnapshotRecord(
            task_id=task_id,
            run_id=run_id,
            tool_call_id="move-then-update",
            path="new.ts",
            op_json={
                "operation": {
                    "type": "UPDATE",
                    "operation_id": "update-moved-file",
                    "tool": "replace",
                },
                "path_states": [
                    {
                        "file_identity": "moved-file",
                        "path": "new.ts",
                        "before": _state(original, original_ref),
                        "after": _state(final),
                    }
                ],
            },
        ),
    ]
    get_file_snapshot_crud().save_batch_with_sequence(task_id, records)
    service = _service(blobs)

    group = service.get_changes(task_id)["files"][0]
    assert group["paths"] == ["new.ts", "old.ts"]
    assert group["action"] == "moved"
    assert group["operation_count"] == 2
    assert group["net_diff"]["state"] == "verified"
    assert "after move" in group["net_diff"]["patch"]

    result = service.apply(task_id, "revert", [group["change_id"]])

    assert result["results"][0]["outcome"] == "reverted"
    assert (root / "old.ts").read_bytes() == original
    assert not (root / "new.ts").exists()


def test_reused_move_source_path_remains_a_separate_file_group(change_set_env) -> None:
    root, task_id, run_id, blobs = change_set_env
    original, replacement = b"moved identity\n", b"new identity\n"
    (root / "old.ts").write_bytes(replacement)
    (root / "new.ts").write_bytes(original)
    original_ref = _put_blob(blobs, original)
    records = [
        FileSnapshotRecord(
            task_id=task_id,
            run_id=run_id,
            tool_call_id="move-source-reuse",
            path="old.ts",
            op_json={
                "operation": {
                    "type": "MOVE",
                    "operation_id": "move-old-file",
                    "tool": "apply_patch",
                },
                "path_states": [
                    {
                        "file_identity": "old-identity",
                        "path": "old.ts",
                        "before": _state(original, original_ref),
                        "after": {"exists": False},
                    },
                    {
                        "file_identity": "old-identity",
                        "path": "new.ts",
                        "before": {"exists": False},
                        "after": _state(original),
                    },
                ],
            },
        ),
        FileSnapshotRecord(
            task_id=task_id,
            run_id=run_id,
            tool_call_id="move-source-reuse",
            path="old.ts",
            op_json={
                "operation": {
                    "type": "ADD",
                    "operation_id": "new-file-at-old-path",
                    "tool": "write_file",
                },
                "path_states": [
                    {
                        "file_identity": "new-identity",
                        "path": "old.ts",
                        "before": {"exists": False},
                        "after": _state(replacement),
                    }
                ],
            },
        ),
    ]
    get_file_snapshot_crud().save_batch_with_sequence(task_id, records)
    service = _service(blobs)
    changes = service.get_changes(task_id)["files"]

    assert len(changes) == 2
    old_group = next(group for group in changes if group["paths"] == ["new.ts", "old.ts"])
    replacement_group = next(group for group in changes if group["paths"] == ["old.ts"])
    assert old_group["net_diff"]["state"] == "conflict"

    result = service.apply(
        task_id,
        "revert",
        [old_group["change_id"], replacement_group["change_id"]],
    )

    assert [item["outcome"] for item in result["results"]] == ["reverted", "reverted"]
    assert (root / "old.ts").read_bytes() == original
    assert not (root / "new.ts").exists()


def test_crash_after_parent_creation_records_the_landed_directory_state(
    change_set_env,
    monkeypatch,
) -> None:
    root, task_id, run_id, blobs = change_set_env
    path = root / "created" / "during" / "crash.txt"
    service = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=run_id,
    )

    class SimulatedCrash(BaseException):
        pass

    def crash_before_replace(_source, _destination):
        raise SimulatedCrash

    monkeypatch.setattr(
        "app.core.tools.tool_handler.patch_write.atomic_write.os.replace",
        crash_before_replace,
    )
    with pytest.raises(SimulatedCrash):
        service.execute_tool_mutation(
            tool_name="write_file",
            arguments={},
            execution_context=context,
            tool_call_id="nested-write-crash",
            write_paths=[path],
            execute=lambda: atomic_write_text(path, "written", containment_root=root),
        )

    assert (root / "created" / "during").is_dir()
    restarted = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    restarted._workspace_root = lambda _workspace_id: root

    assert restarted.reconcile_prepared_snapshots() == []
    assert (root / "created" / "during").is_dir()
    assert not path.exists()


def test_partial_mutation_records_all_observed_changes(change_set_env) -> None:
    root, task_id, run_id, blobs = change_set_env
    first = root / "first.txt"
    second = root / "second.txt"
    first.write_bytes(b"first before")
    second.write_bytes(b"second before")
    service = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=run_id,
    )

    def partially_write():
        atomic_write_text(first, "first after", containment_root=root)
        second.write_bytes(b"human edit")
        atomic_write_text(second, "second after", containment_root=root)
        raise AssertionError("checkpoint must reject the stale second path")

    observation = service.execute_tool_mutation(
        tool_name="write_file",
        arguments={},
        execution_context=context,
        tool_call_id="partial-write",
        write_paths=[first, second],
        execute=partially_write,
    )
    assert observation.status == "error"
    assert first.read_bytes() == b"first after"
    assert second.read_bytes() == b"human edit"

    snapshots = get_file_snapshot_crud().list_any_by_task(task_id)
    assert {snapshot.path for snapshot in snapshots} == {"first.txt", "second.txt"}
    assert {item["paths"][0] for item in _service(blobs).get_changes(task_id)["files"]} == {
        "first.txt",
        "second.txt",
    }
    assert first.read_bytes() == b"first after"
    assert second.read_bytes() == b"human edit"


def test_same_workspace_path_writes_are_serialized_before_snapshot_sequence(
    change_set_env,
) -> None:
    root, task_id, first_run_id, blobs = change_set_env
    second_run = get_conversation_run_crud().create(
        task_id,
        "second concurrent call",
        status="cancelled",
    )
    target = root / "shared.txt"
    target.write_bytes(b"baseline")
    plan = FileToolExecutionPlan(
        resources=FileResourcePaths(write_paths=(target,), lock_paths=(root, target)),
        observed_paths=(),
    )
    first_context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=first_run_id,
    )
    second_context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=second_run.id,
    )
    first_coordinator = FileToolStateCoordinator()
    second_coordinator = FileToolStateCoordinator()
    mutation_service = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    first_entered = threading.Event()
    release_first = threading.Event()
    second_attempting = threading.Event()
    second_entered = threading.Event()
    failures: list[BaseException] = []

    def write(value: bytes, context: ToolExecutionContext, call_id: str) -> None:
        try:
            coordinator = first_coordinator if call_id == "first" else second_coordinator
            with coordinator.lock(plan, context):
                if call_id == "first":
                    first_entered.set()
                    assert release_first.wait(timeout=5)
                else:
                    second_entered.set()

                def handler() -> ToolObservation:
                    atomic_write_text(target, value.decode("utf-8"), preserve_eol=False)
                    return ToolObservation(tool_name="write_file", status="success")

                observation = mutation_service.execute_tool_mutation(
                    tool_name="write_file",
                    arguments={"path": "shared.txt"},
                    execution_context=context,
                    tool_call_id=call_id,
                    write_paths=[target],
                    execute=handler,
                )
                assert observation.status == "success"
        except BaseException as exc:
            failures.append(exc)

    first = threading.Thread(target=write, args=(b"first", first_context, "first"))

    def second_write() -> None:
        second_attempting.set()
        write(b"second", second_context, "second")

    second = threading.Thread(target=second_write)
    first.start()
    assert first_entered.wait(timeout=3)
    second.start()
    assert second_attempting.wait(timeout=3)
    try:
        assert not second_entered.wait(timeout=0.05)
    finally:
        release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert failures == []
    assert second_entered.is_set()
    assert target.read_bytes() == b"second"
    snapshots = sorted(get_file_snapshot_crud().list_any_by_task(task_id), key=lambda row: row.seq)
    assert [row.tool_call_id for row in snapshots] == ["first", "second"]
    first_state = snapshots[0].op_json["path_states"][0]
    second_state = snapshots[1].op_json["path_states"][0]
    assert blobs.read(first_state["before"]["restore_ref"]) == b"baseline"
    assert first_state["after"]["sha256"] == hashlib.sha256(b"first").hexdigest()
    assert second_state["before"]["sha256"] == hashlib.sha256(b"first").hexdigest()
    assert second_state["after"]["sha256"] == hashlib.sha256(b"second").hexdigest()


def test_different_path_writes_reserve_disjoint_task_snapshot_sequences(
    change_set_env,
    monkeypatch,
) -> None:
    root, task_id, run_id, blobs = change_set_env
    paths = [root / "left.txt", root / "right.txt"]
    for path in paths:
        path.write_bytes(b"baseline")
    contexts = [
        ToolExecutionContext(
            task_id=task_id,
            workspace_id=1,
            workspace_root=root,
            run_id=run_id,
        )
        for _ in paths
    ]
    coordinators = [FileToolStateCoordinator() for _ in paths]
    plans = [
        FileToolExecutionPlan(
            resources=FileResourcePaths(write_paths=(path,), lock_paths=(path,)),
            observed_paths=(),
        )
        for path in paths
    ]
    mutation_service = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    create_barrier = threading.Barrier(2)
    original_save = mutation_service._snapshots.save_batch_with_sequence
    observations: list[ToolObservation | None] = [None, None]
    failures: list[BaseException] = []

    def synchronized_save(*args, **kwargs):
        create_barrier.wait(timeout=5)
        return original_save(*args, **kwargs)

    monkeypatch.setattr(mutation_service._snapshots, "save_batch_with_sequence", synchronized_save)

    def write(index: int) -> None:
        path = paths[index]
        value = f"agent-{index}".encode()
        try:
            with coordinators[index].lock(plans[index], contexts[index]):

                def handler() -> ToolObservation:
                    atomic_write_text(path, value.decode(), preserve_eol=False)
                    return ToolObservation(tool_name="write_file", status="success")

                observations[index] = mutation_service.execute_tool_mutation(
                    tool_name="write_file",
                    arguments={"path": path.name},
                    execution_context=contexts[index],
                    tool_call_id=f"parallel-{index}",
                    write_paths=[path],
                    execute=handler,
                )
        except BaseException as exc:
            failures.append(exc)

    workers = [threading.Thread(target=write, args=(index,)) for index in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert all(not worker.is_alive() for worker in workers)
    assert failures == []
    assert [item.status if item else None for item in observations] == ["success", "success"]
    snapshots = sorted(get_file_snapshot_crud().list_any_by_task(task_id), key=lambda row: row.seq)
    assert [row.seq for row in snapshots] == [0, 1]
    assert {row.path for row in snapshots} == {"left.txt", "right.txt"}
    assert {path.read_bytes() for path in paths} == {b"agent-0", b"agent-1"}


def test_external_snapshot_transaction_requires_reserved_sequence(change_set_env) -> None:
    _root, task_id, run_id, _blobs = change_set_env
    record = FileSnapshotRecord(
        task_id=task_id,
        run_id=run_id,
        tool_call_id="external-session",
        path="file.txt",
    )

    with (
        main_session_factory().begin() as session,
        pytest.raises(ValueError, match="seq_start is required"),
    ):
        get_file_snapshot_crud().save_batch_with_sequence(
            task_id,
            [record],
            session=session,
        )

    assert get_file_snapshot_crud().list_any_by_task(task_id) == []


def test_move_crash_records_the_landed_destination(change_set_env) -> None:
    root, task_id, run_id, blobs = change_set_env
    source = root / "before.txt"
    destination = root / "after.txt"
    source.write_bytes(b"move me")
    service = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=run_id,
    )
    class SimulatedCrash(BaseException):
        pass

    def move_then_crash():
        before_file_move(source, destination)
        source.replace(destination)
        raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        service.execute_tool_mutation(
            tool_name="move_file",
            arguments={"source_path": "before.txt", "destination_path": "after.txt"},
            execution_context=context,
            tool_call_id="move-crash",
            write_paths=[source, destination],
            execute=move_then_crash,
        )

    restarted = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    restarted._workspace_root = lambda _workspace_id: root
    assert restarted.reconcile_prepared_snapshots() == []
    assert not source.exists()
    assert destination.read_bytes() == b"move me"
    move_snapshot = get_file_snapshot_crud().list_any_by_task(task_id)[0]
    assert move_snapshot.op_json["operation"]["type"] == "MOVE"
    change_paths = {
        path for group in _service(blobs).get_changes(task_id)["files"] for path in group["paths"]
    }
    assert change_paths == {"before.txt", "after.txt"}


def test_interrupted_move_with_both_paths_present_records_only_the_new_destination(
    change_set_env,
) -> None:
    root, task_id, run_id, blobs = change_set_env
    source = root / "before.txt"
    destination = root / "after.txt"
    source.write_bytes(b"move me")
    service = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=run_id,
    )

    class SimulatedCrash(BaseException):
        pass

    def link_then_crash():
        before_file_move(source, destination)
        os.link(source, destination)
        raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        service.execute_tool_mutation(
            tool_name="move_file",
            arguments={"source_path": "before.txt", "destination_path": "after.txt"},
            execution_context=context,
            tool_call_id="move-link-crash",
            write_paths=[source, destination],
            execute=link_then_crash,
        )

    restarted = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    restarted._workspace_root = lambda _workspace_id: root
    assert restarted.reconcile_prepared_snapshots() == []
    assert source.read_bytes() == b"move me"
    assert destination.read_bytes() == b"move me"
    snapshot = get_file_snapshot_crud().list_any_by_task(task_id)[0]
    assert snapshot.op_json["operation"]["type"] == "ADD"
    assert [path_state["path"] for path_state in snapshot.op_json["path_states"]] == [
        "after.txt"
    ]


def test_delete_file_is_snapshotted_and_revertible(change_set_env) -> None:
    root, task_id, run_id, blobs = change_set_env
    target = root / "obsolete.txt"
    target.write_bytes(b"restore this file\r\n")
    service = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=run_id,
    )
    observation = service.execute_tool_mutation(
        tool_name="delete_file",
        arguments={"path": "obsolete.txt"},
        execution_context=context,
        tool_call_id="apply-patch-delete",
        write_paths=[target],
        execute=lambda: DeleteTool().execute(context, path="obsolete.txt"),
    )

    assert observation.status == "success"
    assert not target.exists()
    # 删除内容只作为回退事实存在：既不进入变更集展示载荷，也不进入工具展示载荷。
    snapshot = get_file_snapshot_crud().list_any_by_task(task_id)[0]
    assert snapshot.op_json["display_patch"] == {
        "format": "deleted",
        "text": "",
        "truncated": False,
    }
    assert observation.display_data["changes"][0]["patch"] is None
    changes = _service(blobs).get_changes(task_id)["files"]
    assert len(changes) == 1
    assert changes[0]["action"] == "deleted"
    assert changes[0]["net_diff"]["patch"] is None
    assert changes[0]["net_diff"]["deletions"] == 1
    assert "restore this file" not in json.dumps(changes)
    result = _service(blobs).apply(task_id, "revert", [changes[0]["change_id"]])
    assert result["results"][0]["outcome"] == "reverted"
    assert target.read_bytes() == b"restore this file\r\n"


def test_delete_file_stages_to_trash_without_blob(change_set_env) -> None:
    root, task_id, run_id, blobs = change_set_env
    target = root / "big.bin"
    target.write_bytes(b"binary-payload")
    service = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=run_id,
    )
    service.execute_tool_mutation(
        tool_name="delete_file",
        arguments={"path": "big.bin"},
        execution_context=context,
        tool_call_id="trash-delete",
        write_paths=[target],
        execute=lambda: DeleteTool().execute(context, path="big.bin"),
    )

    # 删除内容不进入 blob 存储（回退事实在 trash 暂存区，不占内存、不写 blob）。
    assert not list(blobs.blobs_dir.iterdir())
    assert not target.exists()
    snapshot = get_file_snapshot_crud().list_any_by_task(task_id)[0]
    restore_ref = snapshot.op_json["path_states"][0]["before"]["restore_ref"]
    assert restore_ref.startswith("trash:")
    operation_id = restore_ref.split(":", 1)[1].split("/", 1)[0]
    trash_file = root / ".cosir" / "trash" / operation_id / "big.bin"
    assert trash_file.exists()
    assert trash_file.read_bytes() == b"binary-payload"

    # 回退从 trash 把文件移回原路径，并清理 trash 目录。
    change_id = _service(blobs).get_changes(task_id)["files"][0]["change_id"]
    result = _service(blobs).apply(task_id, "revert", [change_id])
    assert result["results"][0]["outcome"] == "reverted"
    assert target.read_bytes() == b"binary-payload"
    assert not (root / ".cosir" / "trash" / operation_id).exists()


def test_delete_file_keep_purges_trash(change_set_env) -> None:
    root, task_id, run_id, blobs = change_set_env
    target = root / "obsolete.txt"
    target.write_bytes(b"gone for good")
    service = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=run_id,
    )
    service.execute_tool_mutation(
        tool_name="delete_file",
        arguments={"path": "obsolete.txt"},
        execution_context=context,
        tool_call_id="keep-delete",
        write_paths=[target],
        execute=lambda: DeleteTool().execute(context, path="obsolete.txt"),
    )
    change_id = _service(blobs).get_changes(task_id)["files"][0]["change_id"]
    result = _service(blobs).apply(task_id, "keep", [change_id])
    assert result["results"][0]["outcome"] == "kept"
    # keep 表示接受删除为新基线：trash 暂存副本彻底清除，文件不再可回退。
    assert not target.exists()
    assert not any((root / ".cosir" / "trash").iterdir())


def test_delete_file_large_binary_reverts_without_blob(change_set_env) -> None:
    root, task_id, run_id, blobs = change_set_env
    payload = bytes((i % 251) for i in range(5_000_000))
    target = root / "large.bin"
    target.write_bytes(payload)
    service = FileMutationService(
        snapshot_crud=get_file_snapshot_crud(),
        blob_store=blobs,
    )
    context = ToolExecutionContext(
        task_id=task_id,
        workspace_id=1,
        workspace_root=root,
        run_id=run_id,
    )
    service.execute_tool_mutation(
        tool_name="delete_file",
        arguments={"path": "large.bin"},
        execution_context=context,
        tool_call_id="large-delete",
        write_paths=[target],
        execute=lambda: DeleteTool().execute(context, path="large.bin"),
    )
    assert not list(blobs.blobs_dir.iterdir())
    assert not target.exists()
    change_id = _service(blobs).get_changes(task_id)["files"][0]["change_id"]
    result = _service(blobs).apply(task_id, "revert", [change_id])
    assert result["results"][0]["outcome"] == "reverted"
    assert target.read_bytes() == payload


def test_active_run_blocks_keep_and_old_group_id_is_idempotent(change_set_env) -> None:
    root, task_id, run_id, blobs = change_set_env
    original, final = b"old\n", b"new\n"
    (root / "note.txt").write_bytes(final)
    _save_transition(task_id, run_id, blobs, identity="file-4", before=original, after=final)
    service = _service(blobs)
    change_id = service.get_changes(task_id)["files"][0]["change_id"]
    get_conversation_run_crud().create(task_id, "active", status="pending")

    blocked = service.apply(task_id, "keep", [change_id])["results"][0]
    assert blocked["outcome"] == "failed"
    assert blocked["reason_code"] == "operation_busy"
    assert get_file_snapshot_crud().list_any_by_task(task_id)[0].status == "pending"
    revert_blocked = service.apply(task_id, "revert", [change_id])["results"][0]
    assert revert_blocked["outcome"] == "failed"
    assert revert_blocked["reason_code"] == "operation_busy"
    assert (root / "note.txt").read_bytes() == final
    assert get_file_snapshot_crud().list_any_by_task(task_id)[0].status == "pending"

    active = get_conversation_run_crud().list_by_task(task_id)[-1]
    from app.models.enums.conversation_run_status import ConversationRunStatus

    get_conversation_run_crud().update_status_if_in(
        active.id,
        ConversationRunStatus.CANCELLED.value,
        (ConversationRunStatus.PENDING.value,),
    )
    assert service.apply(task_id, "keep", [change_id])["results"][0]["outcome"] == "kept"
    assert service.apply(task_id, "keep", [change_id])["results"][0]["outcome"] == "already_kept"
