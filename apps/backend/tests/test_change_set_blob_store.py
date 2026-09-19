from __future__ import annotations

import hashlib

import pytest

import app.service.task.change_set.blob_store as blob_store_module
from app.service.task.change_set.blob_store import (
    BlobIntegrityError,
    ChangeSetBlobStore,
)


def test_blob_store_stages_publishes_and_verifies_exact_bytes(tmp_path) -> None:
    store = ChangeSetBlobStore(tmp_path / "change_set")
    payload = b"\xef\xbb\xbfline one\r\nline two\n"

    staging_id, digest = store.stage(payload)
    assert (store.staging_dir / staging_id).read_bytes() == payload
    restore_ref = store.publish(staging_id, digest)

    assert digest == hashlib.sha256(payload).hexdigest()
    assert restore_ref == f"sha256:{digest}"
    assert store.read(restore_ref) == payload
    assert not (store.staging_dir / staging_id).exists()


def test_blob_store_rejects_corrupt_objects(tmp_path) -> None:
    store = ChangeSetBlobStore(tmp_path / "change_set")
    restore_ref = store.publish(*store.stage(b"original"))
    digest = restore_ref.removeprefix("sha256:")
    (store.blobs_dir / digest).write_bytes(b"corrupt")

    with pytest.raises(BlobIntegrityError):
        store.read(restore_ref)


def test_blob_store_rejects_malformed_restore_reference(tmp_path) -> None:
    store = ChangeSetBlobStore(tmp_path / "change_set")

    with pytest.raises(ValueError, match="invalid ChangeSet restore reference"):
        store.read("sha256:../" + "a" * 61)


def test_blob_store_removes_staging_file_when_flush_fails(tmp_path, monkeypatch) -> None:
    store = ChangeSetBlobStore(tmp_path / "change_set")

    def fail_fsync(_fd: int) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(blob_store_module.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="injected fsync failure"):
        store.stage(b"written before fsync fails")

    assert list(store.staging_dir.iterdir()) == []
