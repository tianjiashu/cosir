"""工具展示契约测试。"""

from pathlib import Path

from app.tools.schemas import ToolExecutionContext
from app.tools.tool_handler.list_directory import ListDirectoryTool
from app.tools.tool_handler.patch_tool import PatchTool
from app.tools.tool_handler.search_files import SearchFilesTool
from app.tools.tool_handler.write_file import WriteFileTool


def _execution_context(workspace_root: Path) -> ToolExecutionContext:
    """构造测试用工具执行上下文。

    参数:
        workspace_root: 临时工作区根目录。

    返回:
        绑定临时工作区的 ``ToolExecutionContext``。

    异常:
        无。

    副作用:
        无。
    """

    return ToolExecutionContext(
        task_id="task-test",
        workspace_id="workspace-test",
        workspace_root=workspace_root,
    )


def test_list_directory_returns_list_display_data(tmp_path: Path) -> None:
    """验证 list_directory 产出前端 list 布局可消费的数据。

    参数:
        tmp_path: pytest 提供的临时目录。

    返回:
        无。

    异常:
        断言失败时由 pytest 抛出。

    副作用:
        在临时目录内创建测试文件和目录。
    """

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "tool_display.py").write_text("class ToolDisplayHints:\n    pass\n")
    (tmp_path / "pkg" / "schemas").mkdir()

    tool = ListDirectoryTool()
    observation = tool.execute("pkg", execution_context=_execution_context(tmp_path))

    assert observation.status == "success"
    assert observation.display_data["entries"] == [
        {
            "name": "schemas",
            "type": "dir",
            "path": "pkg",
            "size": 0,
            "modified": observation.display_data["entries"][0]["modified"],
        },
        {
            "name": "tool_display.py",
            "type": "file",
            "path": "pkg",
            "size": (tmp_path / "pkg" / "tool_display.py").stat().st_size,
            "modified": observation.display_data["entries"][1]["modified"],
        },
    ]

    display = tool.to_definition().display
    assert display is not None
    assert display.render_request({"path": "pkg"})["summary"] == "pkg"
    rendered = tool.render_result_summary(observation.display_data)
    assert rendered[0] == {
        "kind": "directory_entry",
        "icon": "folder",
        "name": "schemas",
        "type": "dir",
        "path": "pkg",
    }


def test_list_directory_empty_result_renders_empty_label(tmp_path: Path) -> None:
    """验证 list_directory 空目录渲染为固定空状态文案。

    参数:
        tmp_path: pytest 提供的临时目录。

    返回:
        无。

    异常:
        断言失败时由 pytest 抛出。

    副作用:
        在临时目录内创建空目录。
    """

    (tmp_path / "empty").mkdir()

    tool = ListDirectoryTool()
    observation = tool.execute("empty", execution_context=_execution_context(tmp_path))

    assert observation.status == "success"
    assert observation.display_data["entries"] == []

    assert tool.render_result_summary(observation.display_data) == "（空目录）"


def test_search_files_returns_content_match_display_data(tmp_path: Path) -> None:
    """验证 search_files 内容搜索产出搜索命中行展示数据。

    参数:
        tmp_path: pytest 提供的临时目录。

    返回:
        无。

    异常:
        断言失败时由 pytest 抛出。

    副作用:
        在临时目录内创建测试文件。
    """

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "tool_display.py").write_text("class ToolDisplayHints:\n    pass\n")

    tool = SearchFilesTool()
    observation = tool.execute(
        pattern="class ToolDisplayHints",
        path="app",
        file_glob="*.py",
        execution_context=_execution_context(tmp_path),
    )

    assert observation.status == "success"
    assert observation.display_data["items"] == [
        {
            "file_path": "tool_display.py",
            "line_number": 1,
            "content": "class ToolDisplayHints:",
        }
    ]

    display = tool.to_definition().display
    assert display is not None
    assert (
        display.render_request(
            {"pattern": "class ToolDisplayHints", "path": "app", "file_glob": "*.py"}
        )["summary"]
        == "class ToolDisplayHints in *.py"
    )
    rendered = tool.render_result_summary(observation.display_data)
    assert rendered == [
        {
            "kind": "content_match",
            "icon": "file",
            "name": "tool_display.py",
            "path": "app",
            "line_number": 1,
            "line_label": "#L1",
        }
    ]


def test_search_files_returns_filename_display_data(tmp_path: Path) -> None:
    """验证 search_files 文件名搜索产出文件结果展示数据。

    参数:
        tmp_path: pytest 提供的临时目录。

    返回:
        无。

    异常:
        断言失败时由 pytest 抛出。

    副作用:
        在临时目录内创建测试文件。
    """

    (tmp_path / "app" / "tools" / "schemas").mkdir(parents=True)
    (tmp_path / "app" / "tools" / "schemas" / "tool_display.py").write_text("")

    tool = SearchFilesTool()
    observation = tool.execute(
        pattern="tool_display.py",
        target="files",
        path="app",
        execution_context=_execution_context(tmp_path),
    )

    assert observation.status == "success"
    assert observation.display_data["items"] == ["tools/schemas/tool_display.py"]

    rendered = tool.render_result_summary(observation.display_data)
    assert rendered == [
        {
            "kind": "file_result",
            "icon": "file",
            "name": "tool_display.py",
            "path": "app/tools/schemas",
            "type": "file",
        }
    ]


def test_search_files_empty_result_renders_empty_label(tmp_path: Path) -> None:
    """验证 search_files 无命中时返回固定空状态文案。

    参数:
        tmp_path: pytest 提供的临时目录。

    返回:
        无。

    异常:
        断言失败时由 pytest 抛出。

    副作用:
        在临时目录内创建测试文件。
    """

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "tool_display.py").write_text("class ToolDisplayHints:\n")

    tool = SearchFilesTool()
    observation = tool.execute(
        pattern="data: dict",
        path="app",
        execution_context=_execution_context(tmp_path),
    )

    assert observation.status == "success"
    assert observation.display_data["items"] == []
    assert tool.render_result_summary(observation.display_data) == "没有搜索到相关内容"


def test_patch_replace_renders_file_diff_entry(tmp_path: Path) -> None:
    """验证 patch replace 模式产出统一文件 diff 展示条目。

    参数:
        tmp_path: pytest 提供的临时目录。

    返回:
        无。

    异常:
        断言失败时由 pytest 抛出。

    副作用:
        在临时目录内创建并修改测试文件。
    """

    target = tmp_path / "app.py"
    target.write_text("print('old')\n")

    tool = PatchTool()
    observation = tool.execute(
        mode="replace",
        path="app.py",
        old_string="print('old')",
        new_string="print('new')",
        execution_context=_execution_context(tmp_path),
    )

    assert observation.status == "success"
    assert "diff" not in observation.display_data["changes"][0]
    assert observation.display_data["diff_stats"]["total_insertions"] == 1
    assert observation.display_data["diff_stats"]["total_deletions"] == 1

    rendered = tool.render_result_summary(observation.display_data)
    assert rendered[0]["kind"] == "file_diff"
    assert rendered[0]["icon"] == "git-compare"
    assert rendered[0]["name"] == "app.py"
    assert rendered[0]["path"] == "app.py"
    assert rendered[0]["status"] == "modified"
    assert rendered[0]["insertions"] == 1
    assert rendered[0]["deletions"] == 1
    assert "-print('old')" in rendered[0]["diff"]
    assert "+print('new')" in rendered[0]["diff"]


def test_write_file_renders_added_file_diff_entry(tmp_path: Path) -> None:
    """验证 write_file 新增文件产出统一文件 diff 展示条目。

    参数:
        tmp_path: pytest 提供的临时目录。

    返回:
        无。

    异常:
        断言失败时由 pytest 抛出。

    副作用:
        在临时目录内写入测试文件。
    """

    tool = WriteFileTool()
    observation = tool.execute(
        path="new_file.py",
        content="print('hello')\n",
        execution_context=_execution_context(tmp_path),
    )

    assert observation.status == "success"
    assert observation.content == "print('hello')\n"
    assert "diff" not in observation.display_data["changes"][0]
    assert observation.display_data["diff_stats"]["total_insertions"] == 1
    assert observation.display_data["diff_stats"]["total_deletions"] == 0

    rendered = tool.render_result_summary(observation.display_data)
    assert rendered[0]["kind"] == "file_diff"
    assert rendered[0]["name"] == "new_file.py"
    assert rendered[0]["path"] == "new_file.py"
    assert rendered[0]["status"] == "added"
    assert rendered[0]["insertions"] == 1
    assert rendered[0]["deletions"] == 0
    assert "+print('hello')" in rendered[0]["diff"]


def test_write_file_renders_modified_file_diff_entry(tmp_path: Path) -> None:
    """验证 write_file 修改文件产出统一文件 diff 展示条目。

    参数:
        tmp_path: pytest 提供的临时目录。

    返回:
        无。

    异常:
        断言失败时由 pytest 抛出。

    副作用:
        在临时目录内创建并修改测试文件。
    """

    target = tmp_path / "existing.py"
    target.write_text("value = 1\n")

    tool = WriteFileTool()
    observation = tool.execute(
        path="existing.py",
        content="value = 2\n",
        execution_context=_execution_context(tmp_path),
    )

    assert observation.status == "success"
    assert observation.content == "value = 2\n"

    rendered = tool.render_result_summary(observation.display_data)
    assert rendered[0]["status"] == "modified"
    assert rendered[0]["insertions"] == 1
    assert rendered[0]["deletions"] == 1
    assert "-value = 1" in rendered[0]["diff"]
    assert "+value = 2" in rendered[0]["diff"]


def test_write_file_diff_metadata_tolerates_non_utf8_existing_file(tmp_path: Path) -> None:
    """验证 write_file 展示元数据不会因旧文件非 UTF-8 而阻断写入。

    参数:
        tmp_path: pytest 提供的临时目录。

    返回:
        无。

    异常:
        断言失败时由 pytest 抛出。

    副作用:
        在临时目录内创建并覆盖测试文件。
    """

    target = tmp_path / "legacy.txt"
    target.write_bytes(b"\xff\xfeold\n")

    tool = WriteFileTool()
    observation = tool.execute(
        path="legacy.txt",
        content="new\n",
        execution_context=_execution_context(tmp_path),
    )

    assert observation.status == "success"
    assert observation.content == "new\n"
    assert target.read_text(encoding="utf-8") == "new\n"

    rendered = tool.render_result_summary(observation.display_data)
    assert rendered[0]["status"] == "modified"
    assert "+new" in rendered[0]["diff"]
