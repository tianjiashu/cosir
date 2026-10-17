"""浏览忽略的跨入口契约；尤其防止误改 Agent 搜索或文件读取权限。"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.app import app
from app.core.tools.schemas import ToolExecutionContext
from app.core.tools.tool_handler.find_files import FindFilesTool
from app.core.tools.tool_handler.search_content import SearchContentTool
from app.service.depends import get_workspace_crud, get_workspace_files_service
from app.service.workspace_files.browser_visibility import BrowserVisibilityStore
from app.service.workspace_files.file_models import FileConflictError
from app.service.workspace_files.workspace_files_service import WorkspaceFilesService


@pytest.fixture
def files(tmp_path, isolated_storage):
    get_workspace_crud().create("demo", str(tmp_path))
    service = WorkspaceFilesService()
    yield service
    service.close()


def search_paths(files, query="*", include_directories=False):
    return {
        entry.path
        for batch in files.search(1, query, threading.Event(), include_directories)
        for entry in batch.entries
    }


def test_missing_settings_are_read_only(files, tmp_path):
    assert files.visibility(1).ignored_paths == ()
    assert not (tmp_path / ".cosir").exists()


def test_exact_subtree_filter_and_preview_survive(files, tmp_path, monkeypatch):
    for name in ["src/a", "src/abc"]:
        (tmp_path / name).mkdir(parents=True)
        (tmp_path / name / "test.py").write_text("needle")
    files.change_visibility(1, ["src/a"], ignored=True)
    page = files.directory(1, "src", None, 10)
    assert [entry.path for entry in page.entries] == ["src/abc"]
    assert page.total == page.hidden_count == 1
    assert files.directory(1, "src/a", None, 10).entries == []
    assert files.info(1, "src/a/test.py").kind == "text"
    assert files.text(1, "src/a/test.py", 0, "utf-8", None, False).text == "needle"
    from app.service.workspace_files import workspace_files_service as module

    scan = module.directory_entries

    def guarded_scan(path, root):
        assert path != tmp_path / "src/a", "被忽略目录不应继续扫描"
        return scan(path, root)

    monkeypatch.setattr(module, "directory_entries", guarded_scan)
    assert search_paths(files, "*.py") == {"src/abc/test.py"}
    candidates = search_paths(files, include_directories=True)
    assert "src/a" not in candidates
    assert "src/abc" in candidates


def test_restart_restore_and_undo_only_actual_changes(files, tmp_path):
    files.change_visibility(1, ["old"], ignored=True)
    result = files.change_visibility(1, ["old", "new", "new"], ignored=True)
    assert result.changed_paths == ("new",)
    store = BrowserVisibilityStore(tmp_path)
    assert store.read().ignored_paths == ("new", "old")
    store.change(list(result.changed_paths), ignored=False)
    assert store.read().ignored_paths == ("old",)
    # 路径不存在也能恢复；重复恢复不产生新变更。
    assert store.change(["new"], ignored=False).changed_paths == ()


def test_paging_invalidated_by_api_and_external_settings(files, tmp_path):
    (tmp_path / "src").mkdir()
    for name in ["a", "b", "c"]:
        (tmp_path / "src" / name).touch()
    first = files.directory(1, "src", None, 1)
    files.change_visibility(1, ["src/b"], ignored=True)
    with pytest.raises(FileConflictError):
        files.directory(1, "src", first.next_cursor, 1)
    first = files.directory(1, "src", None, 1)
    (tmp_path / ".cosir/file-browser.json").write_text(json.dumps({"ignored_paths": ["src/c"]}))
    with pytest.raises(FileConflictError):
        files.directory(1, "src", first.next_cursor, 1)
    assert [e.path for e in files.directory(1, "src", None, 10).entries] == ["src/a", "src/b"]


def test_special_names_are_literals_and_aliases_are_independent(files, tmp_path):
    for name in ["[a].txt", "a.txt", "#x", "!x", "space "]:
        (tmp_path / name).touch()
    (tmp_path / "alias").symlink_to(tmp_path / "a.txt")
    files.change_visibility(1, ["[a].txt", "#x", "!x", "space ", "alias"], ignored=True)
    assert search_paths(files, "*.txt") == {"a.txt"}
    assert "alias" not in search_paths(files)


@pytest.mark.parametrize("path", ["", ".", "./", "../escape", "/absolute", "a/../b", "a\x00b"])
def test_reject_invalid_paths_at_settings_boundary(files, path):
    with pytest.raises(ValueError):
        files.change_visibility(1, [path], ignored=True)


def test_concurrent_changes_preserve_both_updates(tmp_path):
    def update(path):
        BrowserVisibilityStore(tmp_path).change([path], ignored=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(update, ["a", "b"]))
    assert BrowserVisibilityStore(tmp_path).read().ignored_paths == ("a", "b")


def test_browser_settings_never_change_agent_tools(files, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/secret.py").write_text("unique_needle\n")
    files.change_visibility(1, ["src"], ignored=True)
    assert search_paths(files, "*.py") == set()
    context = ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=tmp_path, run_id=1)
    found = FindFilesTool().execute(pattern="*.py", path=".", execution_context=context)
    content = SearchContentTool().execute(
        pattern="unique_needle", path="src", execution_context=context
    )
    assert found.status == content.status == "success"
    assert "src/secret.py" in found.content
    assert "unique_needle" in content.content
    assert (tmp_path / ".cosir/.fileignore").read_text() == ""


def test_http_mutation_and_search_contract(files, tmp_path):
    (tmp_path / "hide.txt").write_text("still readable")
    app.dependency_overrides[get_workspace_files_service] = lambda: files
    try:
        client = TestClient(app)
        url = "/workspaces/1/files"
        assert (
            client.patch(
                url + "/visibility", json={"paths": ["../bad"], "ignored": True}
            ).status_code
            == 400
        )
        response = client.patch(url + "/visibility", json={"paths": ["hide.txt"], "ignored": True})
        assert response.status_code == 200
        assert response.json()["changed_paths"] == ["hide.txt"]
        assert client.get(url + "/visibility").json()["ignored_paths"] == ["hide.txt"]
        batches = client.post(url + "/search", json={"query": "*.txt", "include_directories": True})
        assert all(not batch["entries"] for batch in map(json.loads, batches.text.splitlines()))
        assert client.get(url + "/info", params={"path": "hide.txt"}).status_code == 200
    finally:
        app.dependency_overrides.pop(get_workspace_files_service)
