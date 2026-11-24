"""文件浏览契约：磁盘事实、分页、链接、分段编码、取消和变化轮询。"""

import asyncio
import io
import os
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.app import app
from app.service.depends import get_workspace_crud, get_workspace_files_service
from app.service.workspace_files.content.file_content import (
    FULL_TEXT_BYTES,
    TEXT_CHUNK_BYTES,
    read_text,
)
from app.service.workspace_files.directory.directory_index import DirectoryIndex
from app.service.workspace_files.file_models import FileConflictError, FileEntry
from app.service.workspace_files.file_watch import WatchedPaths
from app.service.workspace_files.search.text_search import search_text
from tests.http_test_support import TRUSTED_HEADERS


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
    monkeypatch.setattr("app.service.workspace_files.directory.directory_index.MEMORY_ENTRIES", 10)
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
        client = TestClient(app, headers=TRUSTED_HEADERS, raise_server_exceptions=True)
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


def test_opened_handle_rejects_link_swapped_after_resolution(files, workspace_root, monkeypatch):
    from app.service.workspace_files.content import file_handle

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

    monkeypatch.setattr(file_handle.os, "open", swap_then_open)
    try:
        with pytest.raises(PermissionError):
            files.info(1, "alias.txt")
    finally:
        outside.unlink()


def test_resource_range_images_and_changed_version(files, workspace_root, monkeypatch):
    from PIL import Image

    from app.service.workspace_files.content import image_preview

    pdf = b"%PDF-1.7\n" + b"a" * 100
    (workspace_root / "test.pdf").write_bytes(pdf)
    Image.new("RGB", (20, 10), "red").save(workspace_root / "picture.png")
    app.dependency_overrides[get_workspace_files_service] = lambda: files
    try:
        client = TestClient(app, headers=TRUSTED_HEADERS)
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
    from app.service.workspace_files.search.search_paging import (
        file_search_pages,
        text_search_pages,
    )

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
    from app.service.workspace_files.directory import directory_entries as entries_module

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

    monkeypatch.setattr(entries_module.os, "open", swap_then_open)
    with pytest.raises(PermissionError):
        files.directory(1, "alias", None, 100)


def test_directory_cancel_releases_scan(files, workspace_root):
    (workspace_root / "one").touch()
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(InterruptedError):
        files.directory(1, "", None, 100, stopped)
    assert len(files._snapshots) == 0


def test_completed_directory_index_survives_request_cancellation(
    files, workspace_root, monkeypatch
):
    monkeypatch.setattr("app.service.workspace_files.directory.directory_index.MEMORY_ENTRIES", 10)
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
        client = TestClient(app, headers=TRUSTED_HEADERS)
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
        client = TestClient(app, headers=TRUSTED_HEADERS)
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
        client = TestClient(app, headers=TRUSTED_HEADERS)
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
        client = TestClient(app, headers=TRUSTED_HEADERS)
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


def test_watched_paths_report_entry_and_content_changes(files, workspace_root):
    """目录的条目增删、文件的内容变化与原子替换都能比对出来；没有变化时不报，比对后基准随之更新。"""
    (workspace_root / "docs").mkdir()
    (workspace_root / "notes.md").write_text("a")
    watched = WatchedPaths.for_request(files, 1, ["docs", "notes.md"])
    root = files.root(1)
    assert {root, root / "docs", root / "notes.md"} <= set(watched.paths)
    assert watched.poll() is False

    (workspace_root / "docs" / "b.md").write_text("b")
    assert watched.poll() is True
    assert watched.poll() is False

    (workspace_root / "notes.md").write_text("longer")
    assert watched.poll() is True

    replacement = workspace_root / "notes.tmp"
    replacement.write_text("longer")
    replacement.replace(workspace_root / "notes.md")
    assert watched.poll() is True


def test_watched_paths_wait_for_missing_paths_and_skip_invalid_ones(files, workspace_root):
    """订阅时还不在的路径出现即通知；越界与写法非法的路径跳过，不影响其余路径。"""
    outside = workspace_root.parent / "outside"
    outside.mkdir()
    (workspace_root / "escape").symlink_to(outside, target_is_directory=True)
    watched = WatchedPaths.for_request(files, 1, ["out/new.md", "../x", "/abs", "escape"])
    root = files.root(1)
    assert set(watched.paths) == {
        root,
        root / ".cosir" / "file-browser.json",
        root / "out" / "new.md",
    }
    (workspace_root / "out").mkdir()
    (workspace_root / "out" / "new.md").write_text("new")
    assert watched.poll() is True


def test_watched_paths_follow_visibility_settings_and_root(files, workspace_root):
    """浏览可见性配置变了目录树要重算；根目录被删时前端要重读出「工作区不在」，两者恒在盯着的集合里。"""
    (workspace_root / "docs").mkdir()
    watched = WatchedPaths.for_request(files, 1, [])
    files.change_visibility(1, ["docs"], ignored=True)
    assert watched.poll() is True
    for child in sorted(workspace_root.rglob("*"), reverse=True):
        child.rmdir() if child.is_dir() else child.unlink()
    workspace_root.rmdir()
    assert watched.poll() is True


@pytest.mark.asyncio
async def test_watched_paths_changes_yield_once_per_changed_period(files, workspace_root, monkeypatch):
    """周期内的多次变化合并为一次产出。"""
    from app.service.workspace_files import file_watch

    monkeypatch.setattr(file_watch, "POLL_INTERVAL_SECONDS", 0.05)
    watched = WatchedPaths.for_request(files, 1, [])
    changes = watched.changes()
    (workspace_root / "a.md").write_text("a")
    (workspace_root / "b.md").write_text("b")
    await asyncio.wait_for(anext(changes), 2)
    pending = asyncio.ensure_future(anext(changes))
    await asyncio.sleep(0.2)
    assert not pending.done()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await changes.aclose()


def test_watch_endpoint_rejects_missing_workspace(files):
    app.dependency_overrides[get_workspace_files_service] = lambda: files
    try:
        client = TestClient(app, headers=TRUSTED_HEADERS)
        response = client.post("/workspaces/99/files/watch", json={"paths": []})
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "WORKSPACE_NOT_FOUND"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("rewrite", ["restore_mtime", "cp_p"])
def test_watched_paths_notice_rewrites_that_keep_mtime(files, workspace_root, rewrite):
    """保留 mtime 的同长改写（改写后恢复 mtime、``cp -p`` 覆盖已有文件）同样通知：比对的就是文件面板的
    版本 ``version_of``，ctime 由系统维护、改不回去。曾经只比 (inode, mtime, size)，这类写入不通知。"""
    import subprocess

    target = workspace_root / "report.md"
    target.write_text("old report")
    before = target.stat()
    watched = WatchedPaths.for_request(files, 1, ["report.md"])
    if rewrite == "restore_mtime":
        target.write_text("new report")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    else:
        source = workspace_root.parent / "source.md"
        source.write_text("new report")
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        subprocess.run(["cp", "-p", str(source), str(target)], check=True)
    after = target.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)
    assert watched.poll() is True
