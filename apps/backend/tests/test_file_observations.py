"""验证文件读取证据与已交付内容及其版本绑定。"""

from pathlib import Path

from app.core.tool.schemas import ToolExecutionContext
from app.tools.tool_handler.apply_patch_tool import ApplyPatchTool
from app.tools.tool_handler.edit_file import EditFileTool
from app.tools.tool_handler.file_edit import patch_apply
from app.tools.tool_handler.read_file import ReadFileTool
from app.tools.tool_handler.write_file import WriteFileTool
from tests.file_knowledge_support import with_read_files
from tests.live_cold_support import LiveColdEnv, tool_call


def _ctx(root: Path, session_id: int = 1) -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id=session_id, workspace_id=1, workspace_root=root, step_id="s", run_id=1
    )


def test_new_file_and_confirmed_own_write_need_no_disk_read(tmp_path: Path) -> None:
    context = _ctx(tmp_path)
    tool = WriteFileTool()
    first = tool.execute("a.txt", "first", context)
    assert first.facts and first.status == "success"
    context.file_knowledge.observe(first.facts, include_reads=False)
    assert tool.execute("a.txt", "again", context).status == "success"


def test_read_in_same_model_batch_does_not_unlock_edit(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("old\n", encoding="utf-8")
    context = _ctx(tmp_path)
    observed = ReadFileTool().execute("a.txt", context)
    assert observed.facts
    context.file_knowledge.observe(observed.facts, include_reads=False)
    edit = EditFileTool()
    assert edit.execute(context, "a.txt", "old", "new").display_data == {"status_hint": "需先读取"}
    context.file_knowledge.observe(observed.facts)
    assert edit.execute(context, "a.txt", "old", "new").status == "success"


def test_entire_file_required_for_overwrite_and_pages_can_be_combined(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    context = _ctx(tmp_path)
    read = ReadFileTool()
    first = read.execute("a.txt", context, limit=1)
    assert first.facts
    context.file_knowledge.observe(first.facts)
    tool = WriteFileTool()
    assert tool.execute("a.txt", "new", context).display_data == {"status_hint": "需读全文"}
    rest = read.execute("a.txt", context, offset=2)
    assert rest.facts
    context.file_knowledge.observe(rest.facts)
    assert tool.execute("a.txt", "new", context).status == "success"


def test_edit_after_partial_read_still_cannot_overwrite(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    context = _ctx(tmp_path)
    first = ReadFileTool().execute("a.txt", context, limit=1)
    assert first.facts
    context.file_knowledge.observe(first.facts)
    edit = EditFileTool().execute(context, "a.txt", "one", "new")
    assert edit.status == "success" and edit.facts
    context.file_knowledge.observe(edit.facts, include_reads=False)
    overwrite = WriteFileTool().execute("a.txt", "replacement", context)
    assert overwrite.display_data == {"status_hint": "需读全文"}


def test_a_change_after_reading_requires_a_new_read_and_does_not_share_sessions(
    tmp_path: Path,
) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old\n", encoding="utf-8")
    context = with_read_files(_ctx(tmp_path), "a.txt")
    tool = WriteFileTool()
    assert tool.execute("a.txt", "other", _ctx(tmp_path, 2)).display_data == {
        "status_hint": "需先读取"
    }
    target.write_text("changed by the user\n", encoding="utf-8")
    assert tool.execute("a.txt", "new", context).display_data == {"status_hint": "文件已变化"}
    assert tool.execute("a.txt", "new", with_read_files(context, "a.txt")).status == "success"


async def test_delivered_read_qualifies_the_next_model_request(
    backend_env, tmp_path, monkeypatch
) -> None:
    target = tmp_path / "input.txt"
    target.write_text("original\n", encoding="utf-8")
    args = {"path": target.name, "content": "replacement"}
    env = LiveColdEnv(
        tmp_path,
        monkeypatch,
        [
            (
                [
                    {
                        "tool_calls": [
                            tool_call("read", "read_file", {"path": target.name}),
                            tool_call("too-early", "write_file", args, index=1),
                        ]
                    }
                ],
                "tool_calls",
            ),
            ([{"tool_calls": [tool_call("retry", "write_file", args)]}], "tool_calls"),
            ([{"content": "done"}], "stop"),
        ],
    )
    try:
        env.send()
        assert (await env.until("completed")).final_output == "done"
        first_results = {
            message["tool_call_id"]: message["content"]
            for message in env.model.requests[1]
            if message["role"] == "tool"
        }
        assert "original" in first_results["read"]
        assert "you have not read" in first_results["too-early"]
        assert target.read_text() == "replacement"
        env.assert_parity()
    finally:
        await env.close()


def test_patch_error_retains_committed_changes(tmp_path, monkeypatch) -> None:
    targets = ("first.txt", "second.txt")
    for name in targets:
        (tmp_path / name).write_text("original\n", encoding="utf-8")
    context = with_read_files(_ctx(tmp_path), *targets)
    write = patch_apply.atomic_write_text

    def fail_second(path, content, **kwargs):
        if path.name == "second.txt":
            raise OSError("second file became unavailable")
        write(path, content, **kwargs)

    monkeypatch.setattr(patch_apply, "atomic_write_text", fail_second)
    patch = "".join(
        f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n"
        "@@ -1 +1 @@\n-original\n+replacement\n"
        for name in targets
    )
    result = ApplyPatchTool().execute(context, patch)
    assert result.display_data["status_hint"] == "部分已写入"
    [mutation] = result.facts.file_mutations
    assert Path(mutation.path).name == "first.txt"
    assert (tmp_path / "second.txt").read_text() == "original\n"
    context.file_knowledge.observe(result.facts, include_reads=False)
    assert WriteFileTool().execute("first.txt", "next", context).status == "success"
