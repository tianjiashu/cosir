"""Capture and restore exact workspace path state for ChangeSet operations."""

from __future__ import annotations

import errno
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from app.core.tools.guard.file_mutation_state import (
    CapturedPath,
    UnsupportedWorkspaceEntry,
    capture_path,
    directory_identity,
    state_matches,
)
from app.core.tools.guard.trash_staging import resolve_trash_path, restore_from_trash
from app.service.task.change_set.blob_store import ChangeSetBlobStore


class WorkspacePathChanged(RuntimeError):
    """Raised when a workspace entry no longer matches its expected pre-restore state."""


def restore_path_state(
    root: Path,
    relative_path: str,
    state: dict[str, Any],
    blob_store: ChangeSetBlobStore,
    *,
    expected_current: dict[str, Any] | None = None,
    expected_directory_identity: str | None = None,
) -> None:
    """Restore one previously captured path state using exact bytes and entry metadata.

    普通文件回退事实有两种来源：``sha256:`` 引用的内容寻址 blob（写入型改写），或 ``trash:``
    引用的落盘暂存副本（删除型操作）。``trash:`` 引用不读取正文，直接把副本移回原路径（同卷 O(1)），
    对大文件/二进制安全；其余仍从 blob 原子写回。符号链接始终由 blob 重建。
    """
    target = _workspace_path(root, relative_path)
    if not state.get("exists"):
        _verify_expected_current(root, target, expected_current)
        if os.path.lexists(target):
            if (
                expected_directory_identity is not None
                and directory_identity(target) != expected_directory_identity
            ):
                raise WorkspacePathChanged(
                    f"workspace directory identity changed before removal: {relative_path}"
                )
            _remove_entry(target)
        return

    entry_type = state.get("entry_type")
    target.parent.mkdir(parents=True, exist_ok=True)
    if entry_type == "directory":
        _verify_expected_current(root, target, expected_current)
        if os.path.lexists(target) and not target.is_dir():
            _remove_entry(target)
        target.mkdir(parents=True, exist_ok=True)
        return
    if entry_type == "file":
        restore_ref = state.get("restore_ref")
        if not isinstance(restore_ref, str):
            raise UnsupportedWorkspaceEntry("file restore object is unavailable")
        trash_path = resolve_trash_path(root, restore_ref)
        if trash_path is not None:
            # 删除类回退事实在 trash 暂存区：把副本移回原路径（同卷 O(1)），不读正文。
            _verify_expected_current(root, target, expected_current)
            restore_from_trash(trash_path, target)
            return
        _atomic_write_bytes(
            target,
            blob_store.read(restore_ref),
            root,
            expected_current=expected_current,
        )
        return
    if entry_type == "symlink":
        restore_ref = state.get("restore_ref")
        if not isinstance(restore_ref, str):
            raise UnsupportedWorkspaceEntry("symbolic link restore object is unavailable")
        link_target = os.fsdecode(blob_store.read(restore_ref))
        _verify_expected_current(root, target, expected_current)
        if os.path.lexists(target):
            _remove_entry(target)
        os.symlink(link_target, target, target_is_directory=bool(state.get("target_is_directory")))
        return
    raise UnsupportedWorkspaceEntry("unknown ChangeSet entry type")


def _workspace_path(root: Path, relative_path: str) -> Path:
    """Resolve a serialized relative path lexically and enforce its workspace boundary."""
    if not relative_path or Path(relative_path).is_absolute() or "\\" in relative_path:
        raise OSError(errno.EPERM, "invalid ChangeSet relative path")
    root_path = root.resolve()
    target = Path(os.path.abspath(root_path / Path(relative_path)))
    try:
        target.relative_to(root_path)
    except ValueError as exc:
        raise OSError(errno.EPERM, "ChangeSet path escapes the workspace") from exc
    try:
        target.parent.resolve(strict=False).relative_to(root_path)
    except (OSError, ValueError) as exc:
        raise OSError(errno.EPERM, "ChangeSet parent escapes the workspace") from exc
    return target


def _remove_entry(path: Path) -> None:
    """Remove a leaf entry without following a symbolic link."""
    metadata = path.lstat()
    if stat.S_ISDIR(metadata.st_mode):
        path.rmdir()
    else:
        path.unlink()


def _atomic_write_bytes(
    path: Path,
    content: bytes,
    containment_root: Path,
    *,
    expected_current: dict[str, Any] | None = None,
) -> None:
    """Replace a regular file atomically after fsyncing an adjacent temporary file."""
    root = containment_root.resolve(strict=True)
    parent = path.parent
    try:
        parent.resolve(strict=False).relative_to(root)
    except (OSError, ValueError) as exc:
        raise OSError(errno.EPERM, "ChangeSet restore parent escapes the workspace") from exc
    parent.mkdir(parents=True, exist_ok=True)
    try:
        parent.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise OSError(errno.EPERM, "ChangeSet restore parent escapes the workspace") from exc
    descriptor, temp_name = tempfile.mkstemp(dir=str(parent), prefix=".tmp_changeset_")
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        try:
            path.resolve(strict=False).relative_to(root)
        except (OSError, ValueError) as exc:
            raise OSError(errno.EPERM, "ChangeSet restore path escapes the workspace") from exc
        _verify_expected_current(root, path, expected_current)
        os.replace(temp_name, path)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise


def _verify_expected_current(
    root: Path,
    path: Path,
    expected_current: dict[str, Any] | None,
) -> None:
    """Recheck a path immediately before a ChangeSet restore mutates its directory entry."""

    if expected_current is None:
        return
    current = capture_path(root, path).state
    if not state_matches(current, expected_current):
        raise WorkspacePathChanged("workspace path changed during ChangeSet restore")


__all__ = [
    "CapturedPath",
    "UnsupportedWorkspaceEntry",
    "WorkspacePathChanged",
    "capture_path",
    "directory_identity",
    "restore_path_state",
    "state_matches",
]
