"""Edge/error-path coverage for ``delete_tool``.

Complements ``test_apply_patch_tool_contract.py`` (which only checks the happy path
and the directory-rejection path).  Focus: cancellation short-circuit, target that
changes before mutation, an ``OSError`` from ``unlink``, and the English/ASCII
contract of the model-facing ``error``/``reason`` strings.
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.tools.schemas import ToolExecutionContext
from app.core.tools.tool_handler.delete_tool import (
    DELETE_FILE_DESCRIPTION,
    DeleteTool,
)


def _context(root: Path, run_id: int = 5150) -> ToolExecutionContext:
    return ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=root, run_id=run_id)


# A blank / device / escape path must be rejected with an English reason and the file
# must survive.
@pytest.mark.parametrize("path", ["", "   ", "/etc/passwd", "NUL", "../escape.txt"])
def test_delete_rejects_invalid_path(tmp_path: Path, path: str) -> None:
    observation = DeleteTool().execute(_context(tmp_path), path=path)

    assert observation.status == "error"
    assert observation.retryable is False
    assert observation.error.isascii()
    assert observation.reason.isascii()


# A missing target must be reported as not-an-existing-regular-file, in English.
def test_delete_missing_file_reports_english_error(tmp_path: Path) -> None:
    observation = DeleteTool().execute(_context(tmp_path), path="nope.txt")

    assert observation.status == "error"
    assert observation.error == "delete target is not an existing regular file"
    assert observation.error.isascii()


# A cancelled run must short-circuit to ``cancelled`` and leave the file on disk.
def test_delete_honours_run_cancellation(tmp_path: Path) -> None:
    target = tmp_path / "keep.txt"
    target.write_bytes(b"keep\n")
    run_id = 7_700_555
    cancellation_registry.clear(run_id)
    context = _context(tmp_path, run_id=run_id)
    cancellation_registry.mark_cancelled(run_id)
    try:
        observation = DeleteTool().execute(context, path="keep.txt")
    finally:
        cancellation_registry.clear(run_id)

    assert observation.status == "cancelled"
    assert target.exists()


# When ``unlink`` fails, the failure must be normalised into a retryable English
# ``tool_error`` and the file must remain readable.
def test_delete_unlink_oserror_is_normalized(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "fail.txt"
    target.write_bytes(b"data\n")
    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if self == target:
            raise OSError(errno.EACCES, "permission denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    observation = DeleteTool().execute(_context(tmp_path), path="fail.txt")

    assert observation.status == "error"
    assert observation.retryable is True
    assert observation.error.startswith("could not delete the target file: ")
    assert observation.error.isascii()
    assert observation.reason.isascii()
    assert target.read_bytes() == b"data\n"


# If the resolved target is swapped out between the pre-check and the mutation
# (destination no longer resolves to the same file), the tool must refuse and report
# the deterministic, non-retryable error rather than deleting the wrong entity.
def test_delete_refuses_target_changed_before_mutation(tmp_path: Path, monkeypatch) -> None:
    import app.core.tools.tool_handler.delete_tool as delete_module

    target = tmp_path / "swap.txt"
    target.write_bytes(b"data\n")
    other = tmp_path / "other.txt"
    other.write_bytes(b"other\n")
    # Patch the guard hook so nothing depends on an active ChangeSet snapshot; the
    # point of this test is the re-resolution comparison, not the guard itself.
    monkeypatch.setattr(delete_module, "before_file_delete", lambda _resolved: None)

    guard_calls: list[Path] = []

    def spy_before_file_delete(resolved):
        guard_calls.append(resolved)

    monkeypatch.setattr(delete_module, "before_file_delete", spy_before_file_delete)

    real_resolve = delete_module.PathResolver.resolve_within_workspace
    calls = {"n": 0}

    def resolving_to_other(self, path):  # noqa: ANN001
        # First call (inside the pre-check) behaves normally; the mutation-time
        # re-resolution returns a *different* path, simulating a concurrent swap.
        resolved, error = real_resolve(self, path)
        calls["n"] += 1
        if calls["n"] >= 2 and resolved is not None and resolved == target.resolve():
            return other.resolve(), ""
        return resolved, error

    monkeypatch.setattr(delete_module.PathResolver, "resolve_within_workspace", resolving_to_other)

    observation = DeleteTool().execute(_context(tmp_path), path="swap.txt")

    assert observation.status == "error"
    assert observation.retryable is False
    assert observation.error == "delete target changed before mutation"
    assert observation.error.isascii()
    assert observation.reason.isascii()
    # Neither file was removed, and the mutation guard was never reached.
    assert target.exists()
    assert other.exists()
    assert guard_calls == []


# The registry description is model-facing and must remain English ASCII prose.
def test_delete_description_is_english_ascii() -> None:
    assert DELETE_FILE_DESCRIPTION.isascii()
    assert "Delete exactly one existing file" in DELETE_FILE_DESCRIPTION
    assert DeleteTool.description == DELETE_FILE_DESCRIPTION


# A successful delete must emit a ``deleted`` file-changes payload that carries only the
# target and its status: the deleted content is a revert fact, never display data.
def test_delete_success_payload(tmp_path: Path) -> None:
    target = tmp_path / "gone.txt"
    target.write_bytes(b"bye\n")

    observation = DeleteTool().execute(_context(tmp_path), path="gone.txt")

    assert observation.status == "success"
    assert observation.content is None
    assert observation.display_data == {
        "kind": "file-changes",
        "changes": [
            {
                "path": "gone.txt",
                "new_path": None,
                "status": "deleted",
                "patch": None,
                "insertions": 0,
                "deletions": 0,
            }
        ],
        "diff_stats": {
            "total_files": 1,
            "total_insertions": 0,
            "total_deletions": 0,
            "files": [
                {
                    "path": "gone.txt",
                    "status": "deleted",
                    "insertions": 0,
                    "deletions": 0,
                    "new_path": None,
                }
            ],
        },
    }
    assert not target.exists()


# Delete only needs to know *which* file is targeted: any read of the target content is a
# regression (a full read of a large file would grow process memory and could escape the
# handler as ``MemoryError``). Binary targets must be deletable as well.
def test_delete_never_reads_the_target_content(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "blob.bin"
    target.write_bytes(b"\x00\x01\x02\xff\xfe")
    real_open = Path.open
    real_read_bytes = Path.read_bytes

    def guarded_open(self, *args, **kwargs):
        if self == target:
            raise AssertionError("delete_file must not open the target file")
        return real_open(self, *args, **kwargs)

    def guarded_read_bytes(self, *args, **kwargs):
        if self == target:
            raise AssertionError("delete_file must not read the target bytes")
        return real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    observation = DeleteTool().execute(_context(tmp_path), path="blob.bin")

    assert observation.status == "success"
    assert observation.display_data["changes"][0]["path"] == "blob.bin"
    assert not target.exists()
