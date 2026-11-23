"""文件工具与 MCP 失败的调用行短提示：按原因分类、不超过 8 字，模型正文不受影响。"""

from pathlib import Path

import pytest

from app.core.tool.results.tool_error import STATUS_HINT_MAX_LENGTH
from app.core.tool.schemas import ToolExecutionContext
from app.tools.mcp.failure import _STATUS_HINTS, MCPFailureKind
from app.tools.tool_handler.file.edit_file import EditFileTool
from app.tools.tool_handler.file.write_file import WriteFileTool
from tests.file_knowledge_support import with_read_files


def _context(root: Path, session_id: int = 41):
    return ToolExecutionContext(
        session_id=session_id,
        run_id=1,
        workspace_root=root,
        workspace_id=1,
        step_id="step",
        tool_call_id="c",
    )


# 目的：编辑失败时用户分得清「原文对不上」与「原文不唯一」；潜在缺陷：一律显示「编辑失败」。
def test_edit_file_failures_name_their_cause(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo\ntwo\n", encoding="utf-8")
    tool = EditFileTool()
    miss = tool.execute(
        path="a.txt",
        old_string="zzz",
        new_string="x",
        execution_context=with_read_files(_context(tmp_path), "a.txt"),
    )
    many = tool.execute(
        path="a.txt",
        old_string="two",
        new_string="x",
        execution_context=with_read_files(_context(tmp_path), "a.txt"),
    )
    same = tool.execute(
        path="a.txt",
        old_string="one",
        new_string="one",
        execution_context=with_read_files(_context(tmp_path), "a.txt"),
    )
    assert (miss.display_data, many.display_data, same.display_data) == (
        {"status_hint": "原文未匹配"},
        {"status_hint": "原文不唯一"},
        {"status_hint": "无需改动"},
    )
    assert "no exact match" in miss.content


# 目的：整文件覆盖被拒时说清是「没读过」还是「读后被改过」；潜在缺陷：两种拒绝说法相同，用户不知道该怎么办。
def test_write_file_refusals_tell_unread_from_changed(tmp_path: Path) -> None:
    target = tmp_path / "b.txt"
    target.write_text("old\n", encoding="utf-8")
    tool = WriteFileTool()
    unread = tool.execute(path="b.txt", content="new\n", execution_context=_context(tmp_path, 42))
    assert unread.display_data == {"status_hint": "需先读取"}
    context = with_read_files(_context(tmp_path, 42), target)
    target.write_text("changed by someone else, longer\n", encoding="utf-8")
    changed = tool.execute(path="b.txt", content="new\n", execution_context=context)
    assert changed.display_data == {"status_hint": "文件已变化"}


@pytest.mark.parametrize("kind", list(MCPFailureKind))
def test_every_mcp_failure_kind_has_a_bounded_hint(kind: MCPFailureKind) -> None:
    assert 0 < len(_STATUS_HINTS[kind]) <= STATUS_HINT_MAX_LENGTH
