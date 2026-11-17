"""文件浏览契约：磁盘事实、分页、链接、分段编码、取消和监听生命周期。"""

import asyncio
import io
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

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


def test_workspace_registered_with_case_variant_stays_readable(workspace_root, isolated_storage):
    """工作区记录的写法与磁盘不一致（大小写变体）时，文件面板照常读写：范围校验按文件身份，不比字符串。

    曾经按句柄取到磁盘写法、再与登记写法做字符串比较，每个文件都 403。
    """
    (workspace_root / "docs").mkdir()
    (workspace_root / "docs" / "a.md").write_text("a")
    variant = workspace_root.parent / workspace_root.name.upper()
    if not variant.exists():
        pytest.skip("filesystem is case-sensitive")
    get_workspace_crud().create("project", str(variant))
    service = get_workspace_files_service()
    try:
        assert service.info(1, "docs/a.md").size == 1
        assert [entry.name for entry in service.directory(1, "docs", None, 10).entries] == ["a.md"]
    finally:
        service.close()


def test_link_cycle_written_in_another_case_is_caught_at_once(files, workspace_root):
    """回归：链接环曾按解析后的路径字符串比较；macOS 上指向祖先的链接换个大小写写法，就多展开一层才被拦住。"""
    (workspace_root / "docs").mkdir()
    if not (workspace_root / "DOCS").exists():
        pytest.skip("filesystem is case-sensitive")
    (workspace_root / "docs" / "loop").symlink_to(workspace_root / "DOCS")
    entries = {entry.name: entry for entry in files.directory(1, "docs", None, 10).entries}
    assert (entries["loop"].kind, entries["loop"].hint) == ("unavailable", "目录链接形成循环")


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
        missing_file = client.get("/workspaces/1/files/info", params={"path": "missing"})
        assert missing_file.status_code == 404
        assert missing_file.json()["detail"]["code"] == "FILE_NOT_FOUND"
        missing_workspace = client.get("/workspaces/2/files/info", params={"path": "data.txt"})
        assert missing_workspace.status_code == 404
        assert missing_workspace.json()["detail"]["code"] == "WORKSPACE_NOT_FOUND"
        assert (
            client.get("/workspaces/1/files/info", params={"path": "../escape"}).status_code == 400
        )
        response = client.get(
            "/workspaces/1/files/resource",
            params={"path": "test.pdf", "version": files.info(1, "test.pdf").version},
            headers={"Range": "bytes=0-7"},
        )
        assert response.status_code == 206
        assert response.content == b"%PDF-1.7"
        data_version = files.info(1, "data.txt").version
        assert (
            client.get(
                "/workspaces/1/files/resource", params={"path": "data.txt", "version": data_version}
            ).status_code
            == 400
        )
    finally:
        app.dependency_overrides.clear()


class _RecordingObserver:
    """替身观察器：只记下交给系统的注册，不碰操作系统的事件机制。

    原生事件的投递时限由操作系统决定、不做保证（macOS 的 fseventsd 满载时可迟到十几秒或丢失），
    测试只验证 cosir 自己的部分：注册了什么、何时释放、事件进来后通知了谁。
    """

    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.schedules: list[str] = []
        self.stopped = False
        # 已退出的监听线程（按注册路径）：模拟根目录被删后 emitter 停止。
        self.dead: set[str] = set()

    @property
    def emitters(self) -> list[SimpleNamespace]:
        """每个注册一条监听线程（与 watchdog 一样以 ``watch`` 对应注册）。"""
        return [SimpleNamespace(watch=watch, is_alive=lambda w=watch: w not in self.dead) for watch in self.handlers]

    def start(self) -> None:
        pass

    def schedule(self, handler, path, recursive=False):
        assert recursive is True
        self.handlers[path] = handler
        self.schedules.append(path)
        return path

    def unschedule(self, watch) -> None:
        del self.handlers[watch]

    def stop(self) -> None:
        self.stopped = True

    def join(self, timeout=None) -> None:
        pass


@pytest.fixture
def recording_observer(monkeypatch):
    from app.service.workspace_files import file_watch

    observers: list[_RecordingObserver] = []

    def make() -> _RecordingObserver:
        observers.append(_RecordingObserver())
        return observers[-1]

    monkeypatch.setattr(file_watch, "Observer", make)
    return observers


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_watch_registers_each_workspace_once_and_releases_when_idle(
    files, workspace_root, recording_observer
):
    """展开目录不再注册：每个工作区只在根上注册一次递归监听；最后一个订阅离开满空闲期才释放。

    前端展开目录是「先断旧连接、再建新连接」，空闲期内的重连不得触发重新注册（注册会冻住整个后端进程）。
    """
    from app.service.workspace_files.file_watch import IDLE_RELEASE_SECONDS

    (workspace_root / "docs").mkdir()
    (workspace_root / "docs" / "a.md").write_text("a")
    clock = _Clock()
    watcher = WorkspaceFileWatcher(clock)
    loop = asyncio.get_running_loop()
    root = files.root(1).resolve()
    try:
        first = watcher.subscribe(1, [""], loop)
        observer = recording_observer[0]
        assert observer.schedules == [str(root)]
        assert first.paths == {root, root / ".cosir"}
        watcher.unsubscribe(first)
        expanded = watcher.subscribe(1, ["docs/a.md", "missing/b.md"], loop)
        assert expanded.paths == {root, root / ".cosir", (workspace_root / "docs").resolve()}
        assert observer.schedules == [str(root)] and watcher.polling(expanded) is False

        watcher.unsubscribe(expanded)
        clock.now += IDLE_RELEASE_SECONDS - 1
        watcher.release_idle()
        assert observer.handlers and not observer.stopped
        clock.now += 1
        watcher.release_idle()
        assert observer.handlers == {} and observer.stopped and watcher._observer is None
    finally:
        watcher.close()


@pytest.mark.asyncio
async def test_watch_close_workspace_releases_registration_immediately(
    files, workspace_root, isolated_storage, recording_observer
):
    """删除工作区立即释放它的注册（不等空闲期），关闭并通知它的订阅；其他工作区的注册与订阅不受影响。"""
    other_root = workspace_root.parent / "other"
    other_root.mkdir()
    other_id = get_workspace_crud().create("other", str(other_root)).id
    watcher = WorkspaceFileWatcher()
    loop = asyncio.get_running_loop()
    try:
        first = watcher.subscribe(1, [""], loop)
        other = watcher.subscribe(other_id, [""], loop)
        observer = recording_observer[0]
        assert set(observer.handlers) == {str(files.root(1).resolve()), str(other_root.resolve())}

        watcher.close_workspace(1)
        await asyncio.wait_for(first.changed.wait(), 1)
        assert first.closed and not other.closed
        assert set(observer.handlers) == {str(other_root.resolve())}
        assert watcher._timers == {} and not observer.stopped

        watcher.close_workspace(other_id)
        assert observer.handlers == {} and observer.stopped and watcher._observer is None
    finally:
        watcher.close()


@pytest.mark.asyncio
async def test_watch_routes_events_to_matching_subscriptions(
    files, workspace_root, recording_observer
):
    """事件按路径找关心它的订阅：所在目录或它本身在过滤集合里才通知；根目录被删时全部通知。"""
    from watchdog.events import DirDeletedEvent, FileModifiedEvent, FileMovedEvent

    (workspace_root / "docs").mkdir()
    (workspace_root / "src" / "deep").mkdir(parents=True)
    watcher = WorkspaceFileWatcher()
    loop = asyncio.get_running_loop()
    root = files.root(1).resolve()
    docs = watcher.subscribe(1, ["docs"], loop)
    src = watcher.subscribe(1, ["src"], loop)
    handler = recording_observer[0].handlers[str(root)]

    async def notified(*subs: object) -> None:
        await asyncio.sleep(0)
        assert {sub for sub in (docs, src) if sub.changed.is_set()} == set(subs)
        docs.changed.clear()
        src.changed.clear()

    try:
        handler.on_any_event(FileModifiedEvent(str(root / "docs" / "a.md")))
        await notified(docs)
        # 递归监听会送来任意深度的变化；没有订阅展开那一层就不推，免得整棵树的变动都触发重读。
        handler.on_any_event(FileModifiedEvent(str(root / "src" / "deep" / "x.py")))
        await notified()
        handler.on_any_event(
            FileMovedEvent(str(root / "docs" / "a.md"), str(root / "src" / "a.md"))
        )
        await notified(docs, src)
        handler.on_any_event(FileModifiedEvent(str(root / ".cosir" / "browse.json")))
        await notified(docs, src)
        handler.on_any_event(DirDeletedEvent(str(root)))
        await notified(docs, src)
    finally:
        watcher.close()
    assert docs.closed and src.closed


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
        pdf_params = {"path": "test.pdf", "version": files.info(1, "test.pdf").version}
        picture_params = {"path": "picture.png", "version": files.info(1, "picture.png").version}
        # 地址按版本寻址：不带版本是调用写错（422），不是读最新。
        assert client.get(url, params={"path": "test.pdf"}).status_code == 422
        for value, expected in [("bytes=-5", pdf[-5:]), ("bytes=5-", pdf[5:])]:
            response = client.get(url, params=pdf_params, headers={"Range": value})
            assert response.status_code == 206
            assert response.content == expected
        assert (
            client.get(url, params=pdf_params, headers={"Range": "bytes=1000-"}).status_code == 416
        )
        assert (
            client.get(
                url, params=pdf_params, headers={"Range": "bytes=0-2", "If-Range": '"old"'}
            ).content
            == pdf
        )
        image = client.get(url, params=picture_params)
        assert image.content == (workspace_root / "picture.png").read_bytes()
        assert image.headers["Cache-Control"] == "private, max-age=31536000, immutable"
        monkeypatch.setattr(image_preview, "THUMBNAIL_PIXELS", 1)
        image = client.get(url, params=picture_params)
        assert "X-Cosir-Image-Preview" not in image.headers
        assert image.headers["Cache-Control"] == "private, max-age=31536000, immutable"
        assert image.content.startswith(b"\x89PNG")
        (workspace_root / "test.pdf").write_bytes(pdf + b"changed")
        stale = client.get(url, params=pdf_params)
        assert stale.status_code == 409
        assert "immutable" not in stale.headers.get("Cache-Control", "")
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
        def __init__(self, watch):
            self.watch = watch

        def is_alive(self):
            return False

    class FailedObserver:
        def __init__(self):
            self.emitters = []
            self.stopped = False

        def start(self):
            pass

        def schedule(self, handler, path, recursive):
            self.emitters.append(DeadEmitter(path))
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
    assert watcher.polling(sub)
    watcher.close()
    assert observer.stopped
    assert watcher._observer is None


@pytest.mark.asyncio
async def test_a_dead_watch_only_degrades_its_own_workspace(
    files, workspace_root, isolated_storage, recording_observer
):
    """回归：一个工作区的监听线程退出（根目录被删时 FSEvents 实测如此），曾把所有工作区的订阅都判成轮询、且永不恢复。"""
    other_root = workspace_root.parent / "other"
    other_root.mkdir()
    other_id = get_workspace_crud().create("other", str(other_root)).id
    watcher = WorkspaceFileWatcher()
    loop = asyncio.get_running_loop()
    try:
        healthy = watcher.subscribe(1, [""], loop)
        broken = watcher.subscribe(other_id, [""], loop)
        assert not watcher.polling(healthy) and not watcher.polling(broken)
        recording_observer[0].dead.add(str(other_root.resolve()))
        assert watcher.polling(broken)
        assert not watcher.polling(healthy)
        # 新来的订阅读的是同一个现场事实：坏掉的工作区一开始就是轮询。
        assert watcher.polling(watcher.subscribe(other_id, [""], loop))
    finally:
        watcher.close()


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


def test_image_info_reports_original_size_or_none(files, workspace_root, monkeypatch):
    """位图的原图尺寸只读文件头；读不出（像素超出安全上限、文件头损坏）时宽高为空，信息接口照常成功。"""
    from PIL import Image

    Image.new("RGB", (20, 10), "red").save(workspace_root / "picture.png")
    (workspace_root / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"not a real png")
    (workspace_root / "data.txt").write_text("text")
    picture = files.info(1, "picture.png")
    assert (picture.kind, picture.width, picture.height) == ("image", 20, 10)
    broken = files.info(1, "broken.png")
    assert (broken.kind, broken.width, broken.height) == ("image", None, None)
    text = files.info(1, "data.txt")
    assert (text.width, text.height) == (None, None)
    # 超过两倍上限时 Pillow 在 open 就抛 DecompressionBombError：调低上限模拟，不真造一张巨图。
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 50)
    bomb = files.info(1, "picture.png")
    assert (bomb.kind, bomb.width, bomb.height) == ("image", None, None)


def test_tiff_is_served_as_png_and_media_with_its_table_type(files, workspace_root):
    """TIFF 内核显示不了，资源接口整张转成 PNG（16 位按取值范围拉伸，不截断发白）；音视频按后缀表给 Content-Type，
    带分段读取，播放器拖动进度靠它。"""
    from PIL import Image

    scan = Image.new("I;16", (8, 4))
    scan.putpixel((0, 0), 1008)
    scan.save(workspace_root / "scan.tif")
    wav = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 40
    (workspace_root / "tone.wav").write_bytes(wav)
    app.dependency_overrides[get_workspace_files_service] = lambda: files
    try:
        client = TestClient(app)
        url = "/workspaces/1/files/resource"
        info = files.info(1, "scan.tif")
        assert (info.kind, info.mime, info.width, info.height) == ("image", "image/tiff", 8, 4)
        image = client.get(url, params={"path": "scan.tif", "version": info.version})
        assert image.headers["content-type"] == "image/png"
        with Image.open(io.BytesIO(image.content)) as png:
            assert png.mode == "L"
            assert png.getextrema() == (0, 255)

        audio = files.info(1, "tone.wav")
        assert (audio.kind, audio.mime) == ("audio", "audio/wav")
        params = {"path": "tone.wav", "version": audio.version}
        whole = client.get(url, params=params)
        assert whole.headers["content-type"] == "audio/wav"
        assert whole.headers["x-content-type-options"] == "nosniff"
        part = client.get(url, params=params, headers={"Range": "bytes=0-3"})
        assert (part.status_code, part.content) == (206, b"RIFF")
    finally:
        app.dependency_overrides.clear()


def test_broken_tiff_is_an_unviewable_image_not_a_server_error(files, workspace_root):
    """像素数据坏了的 TIFF：信息照常返回、宽高为空；资源请求得到明确的读取错误而不是 500。"""
    (workspace_root / "broken.tif").write_bytes(b"II*\x00" + b"\xff" * 64)
    app.dependency_overrides[get_workspace_files_service] = lambda: files
    try:
        client = TestClient(app)
        info = files.info(1, "broken.tif")
        assert (info.kind, info.width) == ("image", None)
        response = client.get(
            "/workspaces/1/files/resource", params={"path": "broken.tif", "version": info.version}
        )
        # 与「图片内容无法识别」同一映射；前端在请求资源前就按宽高为空判为无法预览。
        assert response.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_deliverable_previews_cover_the_tile_and_fit_the_card(files, workspace_root):
    """交付物缩略图只有两种：方块铺满 240×192（长图按短边缩，铺进方块不发糊），大图卡装进 720×480；
    小图不放大、原样返回；动图原样返回保住动画；未知取值与非位图是调用写错。"""
    from PIL import Image

    Image.new("RGB", (1000, 4000), "red").save(workspace_root / "long.png")
    Image.new("RGB", (4000, 1000), "blue").save(workspace_root / "wide.png")
    Image.new("RGB", (200, 150), "green").save(workspace_root / "small.png")
    frames = [Image.new("RGB", (800, 800), color) for color in ("red", "blue")]
    frames[0].save(workspace_root / "anim.gif", save_all=True, append_images=frames[1:])
    (workspace_root / "doc.pdf").write_bytes(b"%PDF-1.7\n" + b"x" * 10)
    app.dependency_overrides[get_workspace_files_service] = lambda: files
    try:
        client = TestClient(app)
        url = "/workspaces/1/files/resource"

        def fetch(path: str, preview: str):
            params = {"path": path, "version": files.info(1, path).version, "preview": preview}
            return client.get(url, params=params)

        def size(response) -> tuple[int, int]:
            with Image.open(io.BytesIO(response.content)) as image:
                return image.size

        tile = fetch("long.png", "tile")
        assert size(tile) == (240, 960)
        assert tile.headers["Cache-Control"] == "private, max-age=31536000, immutable"
        assert size(fetch("wide.png", "tile")) == (768, 192)
        assert size(fetch("wide.png", "card")) == (720, 180)
        assert size(fetch("long.png", "card")) == (120, 480)
        for preview in ("tile", "card"):
            assert (
                fetch("small.png", preview).content == (workspace_root / "small.png").read_bytes()
            )
            assert fetch("anim.gif", preview).content == (workspace_root / "anim.gif").read_bytes()
        assert fetch("small.png", "huge").status_code == 422
        assert fetch("doc.pdf", "tile").status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_previews_keep_exif_orientation(files, workspace_root):
    """竖拍照片（EXIF 方向 6）缩略后仍是竖的：先按方向算目标尺寸再降采样解码。"""
    from PIL import Image

    photo = Image.new("RGB", (1600, 800), "orange")
    exif = photo.getexif()
    exif[0x0112] = 6
    photo.save(workspace_root / "portrait.jpg", exif=exif)
    app.dependency_overrides[get_workspace_files_service] = lambda: files
    try:
        client = TestClient(app)
        params = {
            "path": "portrait.jpg",
            "version": files.info(1, "portrait.jpg").version,
            "preview": "card",
        }
        response = client.get("/workspaces/1/files/resource", params=params)
        with Image.open(io.BytesIO(response.content)) as image:
            assert image.size == (240, 480)
    finally:
        app.dependency_overrides.clear()


def _dispatch_while_holding_watcher_lock(watcher, workspace_id, path, action):
    """在持 ``watcher._lock`` 的线程里，等 watchdog 分发线程（真实观察器、持它自己的锁）进入 ``changed`` 后再做
    ``action``——生产里注册、撤销、停止都是在持锁时调观察器方法。返回该线程是否在时限内完成。"""
    from watchdog.events import FileModifiedEvent

    entered = threading.Event()
    original = watcher.changed

    def changed(workspace, event):
        entered.set()
        original(workspace, event)

    watcher.changed = changed
    done = threading.Event()

    def holder():
        with watcher._lock:
            watch = watcher._registrations[workspace_id].watch
            # 合成事件经观察器自己的事件队列走真实的分发线程，不依赖 FSEvents 的投递时延。
            watcher._observer.event_queue.put((FileModifiedEvent(str(path)), watch))
            assert entered.wait(5)
            time.sleep(0.1)
            action()
        done.set()

    threading.Thread(target=holder, daemon=True).start()
    return done.wait(5)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["register", "release"])
async def test_watch_callback_never_waits_for_the_watcher_lock(
    files, workspace_root, tmp_path, action
):
    """回归：分发线程持观察器锁调回调，我们持 watcher._lock 调 schedule / unschedule / stop。回调若拿锁，两个
    线程互等、永久卡死（曾在自然时序下 15 次循环内复现）。"""
    other = tmp_path / "other"
    other.mkdir()
    get_workspace_crud().create("other", str(other))
    watcher = WorkspaceFileWatcher()
    loop = asyncio.get_running_loop()
    assert not watcher.polling(watcher.subscribe(1, [], loop))

    def run() -> None:
        if action == "register":
            watcher.subscribe(2, [], loop)  # 第二个工作区首次订阅 → schedule
        else:
            watcher._release(1)  # 撤销最后一个注册 → unschedule + stop

    finished = await asyncio.to_thread(
        _dispatch_while_holding_watcher_lock, watcher, 1, workspace_root / "a.txt", run
    )
    # 卡死时锁永远不会放开，收尾的 close() 也会跟着卡住：只有完成了才收尾，失败时让测试立即报告。
    assert finished, "持 watcher._lock 的线程与 watchdog 分发线程互等"
    watcher.close()


@pytest.mark.asyncio
async def test_idle_release_does_not_drop_a_subscription_that_arrives_meanwhile(
    files, recording_observer
):
    """回归：空闲释放选出工作区后若先放锁再撤销，这期间到来的订阅会随旧注册一起被撤掉、静默失去原生监听。"""
    from app.service.workspace_files.file_watch import IDLE_RELEASE_SECONDS

    clock = _Clock()
    watcher = WorkspaceFileWatcher(clock)
    loop = asyncio.get_running_loop()
    watcher.unsubscribe(watcher.subscribe(1, [], loop))
    clock.now += IDLE_RELEASE_SECONDS
    arrived: list = []
    subscriber = threading.Thread(target=lambda: arrived.append(watcher.subscribe(1, [], loop)))
    original = watcher._release

    def release(workspace_id):
        # 撤销前让另一个线程来订阅，给它充足的时间。
        subscriber.start()
        subscriber.join(0.3)
        original(workspace_id)

    watcher._release = release
    await asyncio.to_thread(watcher.release_idle)
    watcher._release = original
    await asyncio.to_thread(subscriber.join, 5)
    assert len(arrived) == 1
    assert not watcher.polling(arrived[0])
    assert 1 in watcher._registrations, "新订阅到来后工作区的原生监听被撤掉了"
    watcher.close()
