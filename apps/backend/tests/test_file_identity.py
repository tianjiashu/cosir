"""路径身份：包含判定按文件身份、不比字符串；规范路径只用于显示。"""

import os
import sys
import unicodedata
from pathlib import Path, PurePath

import pytest

from app.utils.path import file_identity
from app.utils.path.file_identity import (
    FileIdentity,
    RootDirectory,
    UnsupportedFileSystemError,
    canonical_path,
)


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.mkdir()
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.rmdir()


@pytest.fixture
def layout(tmp_path):
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("a")
    (tmp_path / "out").mkdir()
    return tmp_path, root


def test_relative_for_paths_inside_root(layout):
    _, root = layout
    workspace = RootDirectory.at(root)
    assert workspace.relative(root) == PurePath(".")
    assert workspace.relative(root / "src" / "a.py") == PurePath("src", "a.py")
    # 写新文件：尾部不存在的层跳过，按最近存在的祖先判定。
    assert workspace.relative(root / "new" / "deep" / "file") == PurePath("new", "deep", "file")


def test_escapes_are_outside_root(layout):
    """必须先解析再比身份：对原始输入逐级比会把链接与 .. 逃逸误判在内。"""
    base, root = layout
    (root / "link-out").symlink_to(base / "out")
    workspace = RootDirectory.at(root)
    assert workspace.relative(root / ".." / "out" / "x") is None
    assert workspace.relative(root / "nope" / ".." / ".." / "out" / "new") is None
    assert workspace.relative(root / "link-out" / "x") is None
    assert workspace.relative(base / "ws2" / "x") is None


def test_link_into_root_counts_as_inside(layout):
    base, root = layout
    (base / "alias").symlink_to(root / "src")
    assert RootDirectory.at(root).relative(base / "alias" / "a.py") == PurePath("src", "a.py")


def test_case_variants_resolve_to_the_same_root(layout):
    base, root = layout
    if not _case_insensitive(base):
        pytest.skip("文件系统区分大小写，大小写变体是不同目录")
    variant = Path(str(root)[: -len("ws")] + "WS")
    assert RootDirectory.at(root).relative(variant / "src") == PurePath("src")
    assert RootDirectory.at(variant).contains(root / "src" / "a.py")


@pytest.mark.skipif(sys.platform != "darwin", reason="firmlink 只在 macOS 上存在")
def test_firmlink_prefix_resolves_to_the_same_root():
    home = Path.home()
    firmlinked = Path("/System/Volumes/Data") / str(home).lstrip("/")
    if not firmlinked.exists():
        pytest.skip("本机没有 Data 卷 firmlink")
    assert RootDirectory.at(home).relative(firmlinked) == PurePath(".")


def test_unicode_normalization_variants_resolve_to_the_same_directory(tmp_path):
    name = "Café"
    (tmp_path / unicodedata.normalize("NFC", name)).mkdir()
    nfd = tmp_path / unicodedata.normalize("NFD", name)
    if not nfd.exists():
        pytest.skip("文件系统不把 NFC 与 NFD 视为同一个名字")
    assert RootDirectory.at(tmp_path / unicodedata.normalize("NFC", name)).contains(nfd / "x")


def test_hard_link_shares_identity(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    os.link(tmp_path / "a.txt", tmp_path / "b.txt")
    assert FileIdentity.of(tmp_path / "a.txt") == FileIdentity.of(tmp_path / "b.txt")


def test_root_on_volume_without_file_ids_is_rejected(tmp_path, monkeypatch):
    """不提供文件号的卷上身份比较会把整卷判为在根内，必须明确拒绝，不退回字符串比较。"""
    real_stat = os.stat

    class _NoInode:
        def __init__(self, stat: os.stat_result) -> None:
            self.st_dev, self.st_ino = stat.st_dev, 0

    monkeypatch.setattr(file_identity.os, "stat", lambda path: _NoInode(real_stat(path)))
    with pytest.raises(UnsupportedFileSystemError):
        RootDirectory.at(tmp_path)


def test_missing_root_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        RootDirectory.at(tmp_path / "missing")


def test_canonical_path_reports_disk_spelling(layout):
    base, root = layout
    (base / "alias").symlink_to(root / "src")
    assert canonical_path(base / "alias" / "a.py") == canonical_path(root / "src" / "a.py")
    if _case_insensitive(base):
        variant = Path(str(root)[: -len("ws")] + "WS") / "SRC" / "A.py"
        assert canonical_path(variant).name == "a.py"
        assert canonical_path(variant).parent.name == "src"
