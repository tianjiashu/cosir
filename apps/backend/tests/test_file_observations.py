"""write_file 覆盖已有文件前，本 Session 必须看过它、且之后没被别人改过；新建文件不受限。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.tool.schemas import ToolExecutionContext
from app.tools.tool_handler.edit_file import EditFileTool
from app.tools.tool_handler.file_observations import file_observations
from app.tools.tool_handler.read_file import ReadFileTool
from app.tools.tool_handler.write_file import WriteFileTool


@pytest.fixture(autouse=True)
def _forget() -> None:
    yield
    for session_id in (1, 2):
        file_observations.drop_session(session_id)


def _ctx(root: Path, session_id: int = 1) -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id=session_id, workspace_id=1, workspace_root=root, step_id="s", run_id=1, tool_call_id="c"
    )


def _write(root: Path, content: str, session_id: int = 1):
    return WriteFileTool().execute(path="a.txt", content=content, execution_context=_ctx(root, session_id))


def _read(root: Path, session_id: int = 1) -> None:
    assert ReadFileTool().execute(path="a.txt", execution_context=_ctx(root, session_id)).status == "success"


def test_new_files_need_no_read(tmp_path: Path) -> None:
    assert _write(tmp_path, "new").status == "success"
    # 自己刚写过就算看过：接着覆盖不用再读。
    assert _write(tmp_path, "again").status == "success"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "again"


def test_overwriting_an_unread_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("keep", encoding="utf-8")

    refused = _write(tmp_path, "lost")

    assert refused.status == "error" and "have not read it" in (refused.content or "")
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "keep"
    _read(tmp_path)
    assert _write(tmp_path, "replaced").status == "success"


def test_a_change_after_reading_is_refused_until_read_again(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("v1", encoding="utf-8")
    _read(tmp_path)
    target.write_text("v2 by someone else", encoding="utf-8")
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    refused = _write(tmp_path, "mine")

    assert refused.status == "error" and "changed after you last read it" in (refused.content or "")
    assert target.read_text(encoding="utf-8") == "v2 by someone else"
    _read(tmp_path)
    assert _write(tmp_path, "mine").status == "success"


def test_each_session_has_its_own_record(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    _read(tmp_path, session_id=1)

    assert _write(tmp_path, "y", session_id=2).status == "error"
    file_observations.drop_session(1)
    assert _write(tmp_path, "y", session_id=1).status == "error"


def test_edit_file_needs_no_read_and_counts_as_seen(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("old", encoding="utf-8")

    edited = EditFileTool().execute(
        execution_context=_ctx(tmp_path), path="a.txt", old_string="old", new_string="new"
    )

    assert edited.status == "success"
    assert _write(tmp_path, "whole").status == "success"
