"""文件浏览契约：磁盘事实、分页、链接、分段编码、取消和监听生命周期。"""

import asyncio
import os
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.app import app
from app.service.depends import get_workspace_crud, get_workspace_files_service
from app.service.workspace_files.directory_index import DirectoryIndex
from app.service.workspace_files.file_content import FULL_TEXT_BYTES, TEXT_CHUNK_BYTES, read_text
from app.service.workspace_files.file_models import FileConflictError, FileEntry
from app.service.workspace_files.file_watch import WorkspaceFileWatcher
from app.service.workspace_files.text_search import search_text


@pytest.fixture
def workspace_root(tmp_path):
    """工作区根目录与 ``isolated_storage`` 的主库目录（``tmp_path/storage``）分开，避免主库文件混入目录列表。"""
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def files(workspace_root, isolated_storage):
    get_workspace_crud().create("project", str(workspace_root))
    service = get_workspace_files_service()
    yield service
    service.close()


def test_directory_pagination_sort_and_version(files, workspace_root):
    (workspace_root / "folder").mkdir()
    for name in ["file10.py", "file2.py", ".env", "file1.py"]:
        (workspace_root / name).write_text(name)
    first = files.directory(1, "", None, 2)
    assert [entry.name for entry in first.entries] == ["folder", ".env"]
    second = files.directory(1, "", first.next_cursor, 2)
    assert [entry.name for entry in second.entries] == ["file1.py", "file2.py"]
    (workspace_root / "new").touch()
    with pytest.raises(FileConflictError):
        files.directory(1, "", second.next_cursor, 2)
    assert not (workspace_root / ".cosir").exists()


def test_internal_links_external_links_and_cycle(files, workspace_root):
    real = workspace_root / "real"
    real.mkdir()
    (real / "a.py").write_text("print(1)")
    (workspace_root / "alias").symlink_to(real, target_is_directory=True)
    (real / "loop").symlink_to(workspace_root, target_is_directory=True)
    (workspace_root / "outside").symlink_to(workspace_root.parent, target_is_directory=True)
    (workspace_root / "broken").symlink_to(workspace_root / "missing")
    entries = {e.name: e for e in files.directory(1, "", None, 20).entries}
    assert entries["alias"].kind == "directory"
    assert entries["outside"].hint
    assert entries["broken"].hint
    linked = files.directory(1, "alias", None, 20)
    assert next(e for e in linked.entries if e.name == "loop").kind == "unavailable"
    assert files.info(1, "alias/a.py").kind == "text"
    with pytest.raises(PermissionError):
        files.path(1, "outside")
    batches = list(files.search(1, "*.py", threading.Event()))
    assert {e.path for batch in batches for e in batch.entries} == {"real/a.py", "alias/a.py"}
    assert batches[-1].complete
    assert batches[-1].skipped == 4
    assert batches[-1].skipped_reasons == {
        "链接目标位于工作区之外": 1,
        "无法访问或链接已失效": 1,
        "目录链接形成循环": 2,
    }


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16-le", "utf-16-be", "gb18030"])
def test_segmented_read_preserves_multibyte_characters(files, workspace_root, encoding):
    text = "中文 😀 abc\n" * (FULL_TEXT_BYTES // 8)
    if encoding == "gb18030":
        text = "中文 abc\n" * (FULL_TEXT_BYTES // 6)
    path = workspace_root / "large.txt"
    path.write_bytes(text.encode(encoding))
    version = files.info(1, "large.txt").version
    parts, offset = [], 0
    while True:
        page = read_text(path, offset, encoding, version, root=workspace_root)
        parts.append(page.text)
        if page.next_offset is None:
            break
        assert page.next_offset > offset
        offset = page.next_offset
    assert "".join(parts) == text
    tail = read_text(path, 0, encoding, version, True, root=workspace_root)
    assert text.endswith(tail.text)
    path.write_text("changed")
    with pytest.raises(FileConflictError):
        read_text(path, 0, encoding, version, root=workspace_root)


def test_full_text_search_offsets_cross_chunks(files, workspace_root):
    content = "a" * (TEXT_CHUNK_BYTES - 3) + "中文needle结束" + "x" * FULL_TEXT_BYTES
    path = workspace_root / "large.txt"
    path.write_text(content)
    info = files.info(1, "large.txt")
    matches = [
        match
        for batch in search_text(
            path, "中文needle", "utf-8", info.version, threading.Event(), root=workspace_root
        )
        for match in batch["matches"]
    ]
    assert len(matches) == 1
    assert matches[0]["offset"] == TEXT_CHUNK_BYTES - 3


def test_search_cancelled_and_hidden_files(files, workspace_root):
    (workspace_root / ".hidden.py").touch()
    (workspace_root / "generated").mkdir()
    (workspace_root / "generated" / "ignored.py").touch()
    result = list(files.search(1, ".py", threading.Event()))
    assert {e.path for b in result for e in b.entries} == {
        ".hidden.py",
        "generated/ignored.py",
    }
    stopped = threading.Event()
    stopped.set()
    assert list(files.search(1, "*", stopped)) == []


def test_spilled_directory_index_is_sorted_and_cleaned(monkeypatch):
    monkeypatch.setattr("app.service.workspace_files.directory_index.MEMORY_ENTRIES", 10)
    index = DirectoryIndex(FileEntry(str(i), f"file{i}.py", "file") for i in range(300, -1, -1))
    assert index.total == 301
    assert [e.name for e in index.page(9, 3)] == ["file9.py", "file10.py", "file11.py"]
    directory = Path(index._temporary.name)
    index.close()
    assert not directory.exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows 无 FIFO")
def test_special_file_never_blocks(files, workspace_root):
    os.mkfifo(workspace_root / "pipe")
    with pytest.raises(ValueError, match="普通文件"):
        files.info(1, "pipe")


def test_http_contract_and_resource_range(files, workspace_root):
    (workspace_root / "data.txt").write_text("hello")
    (workspace_root / "test.pdf").write_bytes(b"%PDF-1.7\n" + b"x" * 100)
    app.dependency_overrides[get_workspace_files_service] = lambda: files
    try:
        client = TestClient(app, raise_server_exceptions=True)
        assert (
            client.get("/workspaces/1/files/info", params={"path": "data.txt"}).json()["kind"]
            == "text"
        )
        assert client.get("/workspaces/1/files/info", params={"path": "missing"}).status_code == 404
        assert (
            client.get("/workspaces/2/files/info", params={"path": "data.txt"}).status_code == 404
        )
        assert (
            client.get("/workspaces/1/files/info", params={"path": "../escape"}).status_code == 400
        )
        response = client.get(
            "/workspaces/1/files/resource",
            params={"path": "test.pdf"},
            headers={"Range": "bytes=0-7"},
        )
        assert response.status_code == 206
        assert response.content == b"%PDF-1.7"
        assert (
            client.get("/workspaces/1/files/resource", params={"path": "data.txt"}).status_code
            == 400
        )
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_watch_native_update_and_release(files, workspace_root):
    watcher = WorkspaceFileWatcher()
    sub = await asyncio.to_thread(watcher.subscribe, 1, [""], asyncio.get_running_loop())
    try:
        (workspace_root / "new.txt").write_text("new")
        await asyncio.wait_for(sub.changed.wait(), 5)
        assert watcher._watches
        await asyncio.to_thread(watcher.close_workspace, 1)
        assert sub.closed
        assert not watcher._watches
        assert watcher._observer is None
    finally:
        await asyncio.to_thread(watcher.close)


def test_opened_handle_rejects_link_swapped_after_resolution(files, workspace_root, monkeypatch):
    from app.service.workspace_files import file_access

    (workspace_root / "inside.txt").write_text("inside")
    outside = workspace_root.parent / f"{workspace_root.name}-outside.txt"
    outside.write_text("outside")
    link = workspace_root / "alias.txt"
    link.symlink_to(workspace_root / "inside.txt")
    original_open = os.open

    def swap_then_open(path, flags):
        link.unlink()
        link.symlink_to(outside)
        return original_open(path, flags)

    monkeypatch.setattr(file_access.os, "open", swap_then_open)
    try:
        with pytest.raises(PermissionError):
            files.info(1, "alias.txt")
    finally:
        outside.unlink()


def test_resource_range_images_and_changed_version(files, workspace_root, monkeypatch):
    from PIL import Image

    from app.service.workspace_files import image_preview

    pdf = b"%PDF-1.7\n" + b"a" * 100
    (workspace_root / "test.pdf").write_bytes(pdf)
    Image.new("RGB", (20, 10), "red").save(workspace_root / "picture.png")
    app.dependency_overrides[get_workspace_files_service] = lambda: files
    try:
        client = TestClient(app)
        url = "/workspaces/1/files/resource"
        for value, expected in [("bytes=-5", pdf[-5:]), ("bytes=5-", pdf[5:])]:
            response = client.get(url, params={"path": "test.pdf"}, headers={"Range": value})
            assert response.status_code == 206
            assert response.content == expected
        assert (
            client.get(
                url, params={"path": "test.pdf"}, headers={"Range": "bytes=1000-"}
            ).status_code
            == 416
        )
        assert (
            client.get(
                url,
                params={"path": "test.pdf"},
                headers={"Range": "bytes=0-2", "If-Range": '"old"'},
            ).content
            == pdf
        )
        image = client.get(url, params={"path": "picture.png"})
        assert image.content == (workspace_root / "picture.png").read_bytes()
        monkeypatch.setattr(image_preview, "THUMBNAIL_PIXELS", 1)
        image = client.get(url, params={"path": "picture.png"})
        assert image.headers["X-Cosir-Image-Preview"] == "thumbnail"
        assert image.content.startswith(b"\x89PNG")
        version = files.info(1, "test.pdf").version
        (workspace_root / "test.pdf").write_bytes(pdf + b"changed")
        assert client.get(url, params={"path": "test.pdf", "version": version}).status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_search_pages_visit_every_result_with_bounded_windows(files, workspace_root):
    from app.service.workspace_files.search_paging import file_search_pages, text_search_pages

    for number in range(105):
        (workspace_root / f"file{number}.txt").touch()
    entries, offset = [], 0
    while True:
        cancelled = threading.Event()
        batches = list(
            file_search_pages(files.search(1, "*.txt", cancelled), offset, 20, cancelled)
        )
        current = [entry["path"] for batch in batches for entry in batch["entries"]]
        assert len(current) <= 20
        entries.extend(current)
        offset = batches[-1]["next_offset"]
        if offset is None:
            break
    assert len(set(entries)) == 105
    path = workspace_root / "matches.txt"
    path.write_text("中文 needle " * 53)
    info = files.info(1, "matches.txt")
    matches, offset = [], 0
    while True:
        cancelled = threading.Event()
        batches = list(
            text_search_pages(
                search_text(path, "needle", "utf-8", info.version, cancelled, root=workspace_root),
                offset,
                10,
                cancelled,
            )
        )
        current = [match["offset"] for batch in batches for match in batch["matches"]]
        assert len(current) <= 10
        matches.extend(current)
        offset = batches[-1]["next_offset"]
        if offset is None:
            break
    assert len(set(matches)) == 53
    assert all(path.read_bytes()[offset : offset + 6] == b"needle" for offset in matches)


@pytest.mark.asyncio
async def test_watch_degrades_when_emitter_dies_and_releases(files, monkeypatch):
    from app.service.workspace_files import file_watch

    class DeadEmitter:
        def is_alive(self):
            return False

    class FailedObserver:
        def __init__(self):
            self.emitters = [DeadEmitter()]
            self.stopped = False

        def start(self):
            pass

        def schedule(self, handler, path, recursive):
            return path

        def unschedule(self, path):
            pass

        def stop(self):
            self.stopped = True

        def join(self, timeout):
            pass

    observer = FailedObserver()
    monkeypatch.setattr(file_watch, "Observer", lambda: observer)
    watcher = WorkspaceFileWatcher()
    sub = watcher.subscribe(1, [""], asyncio.get_running_loop())
    assert watcher.check_health(sub)
    watcher.unsubscribe(sub)
    assert observer.stopped
    assert watcher._observer is None


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "utf-16-be", "gb18030"])
def test_backward_read_from_eof_preserves_all_text(files, workspace_root, encoding):
    text = "中文😀 line\n" * (FULL_TEXT_BYTES // 8)
    path = workspace_root / "large.txt"
    path.write_bytes(text.encode(encoding))
    version = files.info(1, "large.txt").version
    page = read_text(path, 0, encoding, version, True, root=workspace_root)
    parts = [page.text]
    while page.offset:
        before = page.offset
        page = read_text(path, 0, encoding, version, root=workspace_root, before=before)
        assert page.offset < before
        assert page.next_offset == before
        parts.insert(0, page.text)
    assert "".join(parts) == text


@pytest.mark.skipif(
    os.name == "nt", reason="os.open 竞态注入仅对应 POSIX 目录 fd；Windows 用原生目录句柄"
)
def test_directory_handle_rejects_swapped_link(files, workspace_root, monkeypatch):
    from app.service.workspace_files import file_access

    target = workspace_root / "inside"
    target.mkdir()
    link = workspace_root / "alias"
    link.symlink_to(target, target_is_directory=True)
    original_open = os.open

    def swap_then_open(path, flags):
        if path == link:
            link.unlink()
            link.symlink_to(workspace_root.parent, target_is_directory=True)
        return original_open(path, flags)

    monkeypatch.setattr(file_access.os, "open", swap_then_open)
    with pytest.raises(PermissionError):
        files.directory(1, "alias", None, 100)


def test_directory_cancel_releases_scan(files, workspace_root):
    (workspace_root / "one").touch()
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(InterruptedError):
        files.directory(1, "", None, 100, stopped)
    assert not files._snapshots


def test_completed_directory_index_survives_request_cancellation(files, workspace_root, monkeypatch):
    monkeypatch.setattr("app.service.workspace_files.directory_index.MEMORY_ENTRIES", 10)
    for number in range(300):
        (workspace_root / f"item{number}.txt").touch()
    request_cancelled = threading.Event()
    page = files.directory(1, "", None, 10, request_cancelled)
    request_cancelled.set()
    result = files.directory(1, "", page.next_cursor, 290)
    assert len(result.entries) == 290
    assert result.entries[-1].name == "item299.txt"
