"""Local content-addressed storage for ChangeSet before-images."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from collections.abc import Collection
from contextlib import suppress
from pathlib import Path

from app.config.settings import Settings


class BlobIntegrityError(RuntimeError):
    """Raised when a stored ChangeSet object does not match its content address."""


class ChangeSetBlobStore:
    """Stage and publish exact before-image bytes beneath the local storage directory."""

    def __init__(self, root: Path | None = None) -> None:
        storage_root = root or Settings.DATABASE_FILE.parent / "change_set"
        self.root = Path(storage_root)
        self.blobs_dir = self.root / "blobs"
        self.staging_dir = self.root / "staging"
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)

    def stage(self, content: bytes) -> tuple[str, str]:
        """Write and fsync a temporary copy before a prepared snapshot references it."""
        digest = hashlib.sha256(content).hexdigest()
        staging_id = uuid.uuid4().hex
        path = self.staging_dir / staging_id
        created = False
        try:
            file = path.open("xb")
            created = True
            with file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
        except BaseException:
            # Do not leave a partial object if write/flush/fsync fails. Only unlink
            # after this call created the path; an exclusive-open collision belongs
            # to the existing staging entry and must not remove it.
            if created:
                with suppress(OSError):
                    path.unlink(missing_ok=True)
            raise
        return staging_id, digest

    def publish(self, staging_id: str, digest: str) -> str:
        """Publish a staged object under its SHA-256 address and return its restore ref."""
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("invalid ChangeSet blob digest")
        staged = self.staging_dir / staging_id
        target = self.blobs_dir / digest
        if target.exists():
            self._verify(target, digest)
            staged.unlink(missing_ok=True)
        else:
            self._verify(staged, digest)
            try:
                os.replace(staged, target)
            except OSError:
                # Another operation may have published identical content concurrently.
                if not target.exists():
                    raise
                self._verify(target, digest)
                staged.unlink(missing_ok=True)
        return f"sha256:{digest}"

    def read(self, restore_ref: str) -> bytes:
        """Read and verify a content-addressed restore object."""
        prefix, separator, digest = restore_ref.partition(":")
        if prefix != "sha256" or not separator or not _is_digest(digest):
            raise ValueError("invalid ChangeSet restore reference")
        path = self.blobs_dir / digest
        content = path.read_bytes()
        self._verify_bytes(content, digest)
        return content

    def discard_staging(self, staging_id: str) -> None:
        """Remove a temporary object after its snapshot has been finalized or discarded."""
        if not staging_id or Path(staging_id).name != staging_id:
            raise ValueError("invalid ChangeSet staging id")
        (self.staging_dir / staging_id).unlink(missing_ok=True)

    def collect_unreferenced(self, restore_refs: Collection[str]) -> int:
        """Delete unreferenced regular blob objects; staging is never collected here."""

        referenced = {
            digest
            for restore_ref in restore_refs
            if (digest := _restore_digest(restore_ref)) is not None
        }
        removed = 0
        for path in self.blobs_dir.iterdir():
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(metadata.st_mode) or not _is_digest(path.name):
                continue
            if path.name in referenced:
                continue
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    @staticmethod
    def _verify(path: Path, digest: str) -> None:
        ChangeSetBlobStore._verify_bytes(path.read_bytes(), digest)

    @staticmethod
    def _verify_bytes(content: bytes, digest: str) -> None:
        if hashlib.sha256(content).hexdigest() != digest:
            raise BlobIntegrityError("ChangeSet object failed its SHA-256 check")


def _is_digest(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _restore_digest(restore_ref: str) -> str | None:
    prefix, separator, digest = restore_ref.partition(":")
    return digest if prefix == "sha256" and separator and _is_digest(digest) else None
