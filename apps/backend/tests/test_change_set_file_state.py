from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.service.task.change_set.file_state as file_state
from app.service.task.change_set.blob_store import ChangeSetBlobStore
from app.service.task.change_set.file_state import (
    UnsupportedWorkspaceEntry,
    WorkspacePathChanged,
    capture_path,
    directory_identity,
    restore_path_state,
    state_matches,
)


def test_capture_and_restore_preserves_original_file_bytes(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "src" / "sample.txt"
    path.parent.mkdir()
    original = b"\xef\xbb\xbfalpha\r\nbeta\n"
    path.write_bytes(original)
    store = ChangeSetBlobStore(tmp_path / "storage" / "change_set")

    captured = capture_path(root, path)
    assert captured.content is not None
    before = {**captured.state, "restore_ref": store.publish(*store.stage(captured.content))}
    path.write_bytes(b"modified")

    assert before["sha256"] == hashlib.sha256(original).hexdigest()
    assert store.read(before["restore_ref"]) == original
    restore_path_state(root, captured.path, before, store)
    assert path.read_bytes() == original
    assert state_matches(capture_path(root, path).state, before)


def test_restore_rechecks_external_edit_after_staging_before_replace(tmp_path, monkeypatch) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "sample.txt"
    path.write_bytes(b"baseline")
    store = ChangeSetBlobStore(tmp_path / "storage" / "change_set")
    captured_baseline = capture_path(root, path)
    assert captured_baseline.content is not None
    baseline = {
        **captured_baseline.state,
        "restore_ref": store.publish(*store.stage(captured_baseline.content)),
    }
    expected_current = {
        "exists": True,
        "entry_type": "file",
        "sha256": hashlib.sha256(b"agent").hexdigest(),
    }
    verify_expected_current = file_state._verify_expected_current
    injected = False

    def edit_after_staging_then_verify(root_path, target_path, expected_state) -> None:
        nonlocal injected
        if not injected:
            path.write_bytes(b"human edit")
            injected = True
        verify_expected_current(root_path, target_path, expected_state)

    monkeypatch.setattr(file_state, "_verify_expected_current", edit_after_staging_then_verify)

    with pytest.raises(WorkspacePathChanged):
        restore_path_state(
            root,
            "sample.txt",
            baseline,
            store,
            expected_current=expected_current,
        )

    assert path.read_bytes() == b"human edit"
    assert not list(root.glob(".tmp_changeset_*"))


def test_directory_removal_rejects_replaced_directory_identity(tmp_path) -> None:
    root = tmp_path / "workspace"
    directory = root / "created"
    directory.mkdir(parents=True)
    store = ChangeSetBlobStore(tmp_path / "storage" / "change_set")
    original_identity = directory_identity(directory)
    assert original_identity is not None

    directory.rmdir()
    directory.mkdir()

    with pytest.raises(WorkspacePathChanged):
        restore_path_state(
            root,
            "created",
            {"exists": False},
            store,
            expected_current={"exists": True, "entry_type": "directory"},
            expected_directory_identity=original_identity,
        )

    assert directory.is_dir()


def test_symlink_capture_and_restore_preserve_link_target_and_directory_mode(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    link = root / "directory-link"
    link_target = "../external-target"
    store = ChangeSetBlobStore(tmp_path / "storage" / "change_set")
    real_lexists = os.path.lexists
    real_lstat = Path.lstat
    real_stat = Path.stat
    real_readlink = os.readlink

    with monkeypatch.context() as patcher:
        patcher.setattr(
            os.path,
            "lexists",
            lambda value: Path(value) == link or real_lexists(value),
        )
        patcher.setattr(
            Path,
            "lstat",
            lambda self: (
                SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)
                if self == link
                else real_lstat(self)
            ),
        )
        patcher.setattr(
            Path,
            "stat",
            lambda self, *args, **kwargs: (
                SimpleNamespace(st_mode=stat.S_IFDIR | 0o777)
                if self == link
                else real_stat(self, *args, **kwargs)
            ),
        )
        patcher.setattr(
            os,
            "readlink",
            lambda path: link_target if Path(path) == link else real_readlink(path),
        )
        captured = capture_path(root, link)

    before = {
        **captured.state,
        "restore_ref": store.publish(*store.stage(os.fsencode(captured.state["target"]))),
    }
    assert before["target"] == link_target
    assert before["target_is_directory"] is True
    assert store.read(before["restore_ref"]) == os.fsencode(link_target)

    created: list[tuple[str, Path, bool]] = []
    monkeypatch.setattr(
        os,
        "symlink",
        lambda target, destination, *, target_is_directory: created.append(
            (target, Path(destination), target_is_directory)
        ),
    )
    restore_path_state(root, captured.path, before, store)
    assert created == [(link_target, link, True)]


def test_capture_rejects_windows_reparse_point(tmp_path, monkeypatch) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "junction-like-entry"
    real_lexists = os.path.lexists
    real_lstat = Path.lstat
    monkeypatch.setattr(
        os.path,
        "lexists",
        lambda value: Path(value) == target or real_lexists(value),
    )
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda self: (
            SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o700,
                st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
            )
            if self == target
            else real_lstat(self)
        ),
    )

    with pytest.raises(UnsupportedWorkspaceEntry, match="unsupported reparse point"):
        capture_path(root, target)
