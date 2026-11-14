"""read_file 的分页契约：页头报总行数、页尾给续读 offset 或文件尾、整页不超模型通道上限。

模型据这些事实决定是否继续读，不必再用 shell 数行数（设计文档 A2）。展示数据沿用
``tool_ui_display_contract.md`` 3.1：读到尾 ``END``，只有字符预算截断才带 ``truncated``。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.config.constant import Constant
from app.tools.tool_handler.read_file import ReadFileTool


def _ctx(root: Path) -> SimpleNamespace:
    return SimpleNamespace(workspace_root=root, session_id=1)


def _write_lines(root: Path, name: str, count: int, width: int = 6) -> Path:
    path = root / name
    path.write_text("\n".join(f"L{i}".ljust(width, "x") for i in range(1, count + 1)), "utf-8")
    return path


def test_page_header_reports_total_lines_and_footer_gives_next_offset(tmp_path: Path) -> None:
    _write_lines(tmp_path, "a.txt", 1000)

    observation = ReadFileTool().execute(path="a.txt", execution_context=_ctx(tmp_path), limit=3)

    assert observation.status == "success"
    lines = observation.content.splitlines()
    assert lines[0] == "[lines 1-3 of 1000]"
    assert lines[1:4] == ["1| L1xxxx", "2| L2xxxx", "3| L3xxxx"]
    assert lines[-1] == "[continue with offset=4]"
    assert observation.display_data["line_range"] == {"start": 1, "end": 3}
    assert "truncated" not in observation.display_data


def test_reading_to_the_end_marks_end_of_file(tmp_path: Path) -> None:
    _write_lines(tmp_path, "a.txt", 10)

    observation = ReadFileTool().execute(
        path="a.txt", execution_context=_ctx(tmp_path), offset=8, limit=50
    )

    lines = observation.content.splitlines()
    assert lines[0] == "[lines 8-10 of 10]"
    assert lines[-1] == "[end of file]"
    assert observation.display_data["line_range"] == {"start": 8, "end": "END"}


def test_character_budget_pages_inside_the_tool_below_the_output_limit(tmp_path: Path) -> None:
    """长行文件在字符预算处整行停下：结果不会再被 ToolOutputBudget 中段截断，且标 truncated。"""

    _write_lines(tmp_path, "big.txt", 1000, width=100)

    observation = ReadFileTool().execute(
        path="big.txt", execution_context=_ctx(tmp_path), limit=2000
    )

    assert len(observation.content) <= Constant.Tools.MAX_OUTPUT_CHARS
    lines = observation.content.splitlines()
    assert lines[0].startswith("[lines 1-") and lines[0].endswith(" of 1000]")
    assert lines[-1].startswith("[stopped at the character budget; continue with offset=")
    next_offset = int(lines[-1].rsplit("=", 1)[1].rstrip("]"))
    assert lines[-2].startswith(f"{next_offset - 1}| ")
    assert observation.display_data["truncated"] is True
    assert observation.display_data["status_hint"] == "文件过大，已截断"


def test_empty_file_and_offset_past_end_state_the_fact(tmp_path: Path) -> None:
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    _write_lines(tmp_path, "a.txt", 5)
    tool = ReadFileTool()

    empty = tool.execute(path="empty.txt", execution_context=_ctx(tmp_path))
    past = tool.execute(path="a.txt", execution_context=_ctx(tmp_path), offset=9)

    assert empty.content == "[empty file]\n[end of file]"
    assert empty.display_data["line_range"] == {"start": 1, "end": "END"}
    assert past.content == "[offset 9 is past the end: the file has 5 lines]\n[end of file]"
    assert past.display_data["line_range"] == {"start": 9, "end": "END"}


def test_description_states_the_real_budget_and_paging_facts() -> None:
    description = ReadFileTool().description

    assert f"about {ReadFileTool.max_content_chars // 1000}K characters" in description
    assert "100K" not in description
    assert "`lines a-b of N`" in description
    assert "`[end of file]`" in description
    assert ReadFileTool.max_content_chars < Constant.Tools.MAX_OUTPUT_CHARS


def test_read_only_tools_are_scheduled_in_parallel() -> None:
    """只读工具声明并行：模型一次发出的多个读取真正并发执行（设计文档 A3）。"""

    from app.core.tool.handler.web_extract import WebExtractTool
    from app.core.tool.handler.web_search import WebSearchTool
    from app.tools.tool_handler.find_files import FindFilesTool
    from app.tools.tool_handler.read_image import ReadImageTool
    from app.tools.tool_handler.search_content import SearchContentTool

    definitions = [
        ReadFileTool().to_definition(),
        ReadImageTool().to_definition(),
        SearchContentTool().to_definition(),
        FindFilesTool().to_definition(),
        WebSearchTool(object()).to_definition(),  # type: ignore[arg-type]
        WebExtractTool(object()).to_definition(),  # type: ignore[arg-type]
    ]

    assert {definition.parallel_mode for definition in definitions} == {"parallel"}
