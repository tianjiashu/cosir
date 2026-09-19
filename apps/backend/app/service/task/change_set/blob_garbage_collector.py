"""Transactional garbage collection for unreferenced ChangeSet before-images."""

from __future__ import annotations

import json
from typing import Any

from app.service.task.change_set.blob_store import ChangeSetBlobStore
from app.storage.crud.file_snapshot_crud import FileSnapshotCrud
from app.storage.store_engines import main_session_factory
from app.storage.write_transaction import begin_immediate


class ChangeSetBlobGarbageCollector:
    """Delete zero-reference blobs while SQLite prevents concurrent reference creation."""

    def __init__(
        self,
        blob_store: ChangeSetBlobStore,
        snapshot_crud: FileSnapshotCrud,
    ) -> None:
        self._blobs = blob_store
        self._snapshots = snapshot_crud

    def collect(self) -> int:
        """Remove unreferenced objects under a SQLite immediate write transaction.

        Staging files are outside the blob object directory and are never considered here.
        Prepared snapshots protect before-images until mutation reconciliation completes.
        """

        with begin_immediate(main_session_factory()) as session:
            references: set[str] = set()
            for envelope_json in self._snapshots.list_pending_op_json(session):
                _collect_restore_refs(_parse_json(envelope_json), references)
            for mutation_json in self._snapshots.list_unfinalized_mutation_json(session):
                _collect_restore_refs(_parse_json(mutation_json), references)
            return self._blobs.collect_unreferenced(references)


def _parse_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def _collect_restore_refs(value: Any, references: set[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "restore_ref" and isinstance(child, str):
                references.add(child)
            _collect_restore_refs(child, references)
    elif isinstance(value, list):
        for child in value:
            _collect_restore_refs(child, references)
