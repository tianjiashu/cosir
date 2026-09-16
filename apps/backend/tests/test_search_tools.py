from pathlib import Path

from app.core.tools.schemas import ToolExecutionContext
from app.core.tools.tool_handler.find_files import FindFilesTool
from app.core.tools.tool_handler.search.content_engine import search_content
from app.core.tools.tool_handler.search.result import ContentSearchPage
from app.core.tools.tool_handler.search.scope import SearchScope
from app.core.tools.tool_handler.search_content import SearchContentTool


def _context(root: Path, *, run_id: int = 1) -> ToolExecutionContext:
    return ToolExecutionContext(
        task_id=1,
        workspace_id=1,
        workspace_root=root,
        run_id=run_id,
    )


def test_search_scope_accepts_file_and_directory(tmp_path: Path) -> None:
    source = tmp_path / "src" / "main.py"
    source.parent.mkdir()
    source.write_text("needle\nother\n", encoding="utf-8")

    file_scope = SearchScope.from_path(tmp_path, source)
    directory_scope = SearchScope.from_path(tmp_path, source.parent)

    assert file_scope.kind == "file"
    assert list(file_scope.iter_files()) == [source]
    assert directory_scope.kind == "directory"
    assert list(directory_scope.iter_files()) == [source]


def test_search_content_supports_a_single_file(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("before\nneedle here\nafter\n", encoding="utf-8")

    observation = SearchContentTool().execute(
        pattern="needle",
        path="main.py",
        execution_context=_context(tmp_path),
    )

    assert observation.status == "success"
    assert "main.py:2:>needle here" in (observation.content or "")
    assert observation.display_data["kind"] == "content-search-results"
    assert observation.display_data["matches"] == [
        {"path": "main.py", "line": 2, "content": "needle here", "is_match": True}
    ]


def test_search_content_recurses_directory_and_skips_binary(tmp_path: Path) -> None:
    source = tmp_path / "src" / "main.py"
    source.parent.mkdir()
    source.write_text("needle\n", encoding="utf-8")
    (source.parent / "image.bin").write_bytes(b"needle\x00binary")

    observation = SearchContentTool().execute(
        pattern="needle",
        path="src",
        execution_context=_context(tmp_path),
    )

    assert observation.status == "success"
    assert "src/main.py:1:>needle" in (observation.content or "")
    assert observation.display_data["skipped_files"] == 1


def test_search_content_returns_structured_engine_page_and_paginates(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("needle one\nneedle two\n", encoding="utf-8")
    scope = SearchScope.from_path(tmp_path, source.parent)

    page = search_content(scope, "needle", limit=1, offset=1)

    assert isinstance(page, ContentSearchPage)
    assert page.total_rows == 2
    assert page.match_count == 2
    assert len(page.matches) == 1
    assert page.matches[0].line_number == 2


def test_search_content_rejects_invalid_path(tmp_path: Path) -> None:
    observation = SearchContentTool().execute(
        pattern="needle",
        path="missing.py",
        execution_context=_context(tmp_path),
    )

    assert observation.status == "error"
    assert observation.retryable is True
    assert "existing file or directory" in (observation.reason or "")


def test_search_content_has_cooperative_thread_timeout(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "main.py"
    source.write_text("needle\n" * 20, encoding="utf-8")
    clock = iter([0.0, 0.0, 0.0, 0.0, 31.0])
    monkeypatch.setattr(
        "app.core.tools.tool_handler.search.content_engine.time.monotonic",
        lambda: next(clock),
    )

    observation = SearchContentTool().execute(
        pattern="needle",
        path=".",
        execution_context=_context(tmp_path),
    )

    assert observation.status == "error"
    assert observation.display_data["status_hint"] == "搜索超时"


def test_find_files_supports_a_single_file(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("x\n", encoding="utf-8")

    observation = FindFilesTool().execute(
        pattern="*.py",
        path="main.py",
        execution_context=_context(tmp_path),
    )

    assert observation.status == "success"
    assert observation.content == "main.py"
    assert observation.display_data["kind"] == "file-list"


def test_search_tool_schemas_are_split_and_thread_based() -> None:
    content_definition = SearchContentTool().to_definition()
    files_definition = FindFilesTool().to_definition()

    assert content_definition.name == "search_content"
    assert files_definition.name == "find_files"
    assert content_definition.execution_mode == "thread"
    assert files_definition.execution_mode == "thread"
    assert "target" not in content_definition.args_model.model_json_schema()["properties"]
    assert "target" not in files_definition.args_model.model_json_schema()["properties"]


def test_find_files_has_cooperative_thread_timeout(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "main.py").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(FindFilesTool, "timeout_seconds", 0.0)

    observation = FindFilesTool().execute(
        pattern="*.py",
        path=".",
        execution_context=_context(tmp_path),
    )

    assert observation.status == "error"
    assert observation.display_data["status_hint"] == "搜索超时"
