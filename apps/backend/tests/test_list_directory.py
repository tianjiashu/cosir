"""list_directory 工具单元测试（仅后端）。

覆盖本次修复的正确性缺陷：
- #5 条目 path 为「条目自身」的真实绝对路径（含文件名），不依赖模型原始输入。
- #6 指向目录的符号链接分类为 "link" 而非误判 "file"；损坏符号链接不崩溃。
- #4 offset 越界给出明确提示，不静默伪装成空目录（与真空目录区分）。
- 基础：正常列举、name 级 glob、空目录。

注意：``os.scandir`` 仅列直接子项，故 ``include_globs`` 只匹配条目名（不带目录前缀），
相关语义在 handler 的 description/docstring 中明确，本测试只验证 name 级匹配。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.tools.tool_handler.list_directory import ListDirectoryTool


@pytest.fixture
def populated(workspace: Path) -> Path:
    """构造一个含子目录/文件/嵌套 py 的 workspace 用于列举。"""
    (workspace / "src").mkdir()
    (workspace / "src" / "a.py").write_text("x")
    (workspace / "src" / "b.txt").write_text("y")
    (workspace / "top.py").write_text("z")
    return workspace


def test_path_is_entry_real_absolute_path(populated: Path, context) -> None:
    """条目 path 应为「条目自身」的真实绝对路径（含文件名），不依赖模型原始输入（#5）。"""
    result = ListDirectoryTool().execute(path=".", execution_context=context)
    assert result.status == "success"
    entries = (result.data or {}).get("entries", [])
    assert entries, "expected at least one entry"
    resolved_root = populated.resolve().as_posix()
    for entry in entries:
        p = Path(entry["path"])
        assert p.is_absolute(), f"path should be resolved absolute, got {entry['path']}"
        # 真实修复点：path 必须以 resolved 根为前缀，且包含条目自身文件名，
        # 而非退化为「目录路径」导致前端拼接时仍需自行加 name。
        assert entry["path"].startswith(
            resolved_root
        ), f"path should live under workspace root, got {entry['path']}"
        assert entry["path"].endswith(
            entry["name"]
        ), f"path should include entry name, got {entry['path']} vs {entry['name']}"


def test_include_globs_matches_name_only(populated: Path, context) -> None:
    """include_globs 按条目名正向白名单（'*.py' 只展示 top.py，排除 src 目录）。"""
    result = ListDirectoryTool().execute(
        path=".", execution_context=context, include_globs=["*.py"]
    )
    assert result.status == "success"
    names = {e["name"] for e in (result.data or {}).get("entries", [])}
    assert names == {"top.py"}, f"only *.py should be shown by include_globs, got {names}"


def test_include_globs_empty_means_no_filter(populated: Path, context) -> None:
    """include_globs 为空时不做名字过滤，展示全部（仍受 include_hidden 控制）。"""
    result = ListDirectoryTool().execute(path=".", execution_context=context, include_globs=[])
    assert result.status == "success"
    names = {e["name"] for e in (result.data or {}).get("entries", [])}
    assert {"src", "top.py"}.issubset(
        names
    ), f"empty include_globs should show all entries, got {names}"


def test_symlink_to_dir_classified_as_link(populated: Path, context) -> None:
    """指向目录的符号链接分类为 'link'（#6），不误判为 file。"""
    link = populated / "src_link"
    try:
        os.symlink(populated / "src", link, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlink not supported on this platform")
    result = ListDirectoryTool().execute(path=".", execution_context=context, include_hidden=True)
    assert result.status == "success"
    entries = (result.data or {}).get("entries", [])
    link_entry = next((e for e in entries if e["name"] == "src_link"), None)
    assert link_entry is not None, "symlink entry should be listed"
    assert (
        link_entry["type"] == "link"
    ), f"symlink to dir should be 'link', got {link_entry['type']}"


def test_broken_symlink_does_not_crash(populated: Path, context) -> None:
    """指向不存在目标的损坏符号链接被安全列出且分类为 link，不应抛异常或崩溃。"""
    broken = populated / "broken_link"
    try:
        os.symlink(populated / "does_not_exist_xyz", broken)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlink not supported on this platform")
    result = ListDirectoryTool().execute(path=".", execution_context=context, include_hidden=True)
    assert (
        result.status == "success"
    ), f"listing should not crash on broken symlink: {result.error!r}"
    entries = (result.data or {}).get("entries", [])
    broken_entry = next((e for e in entries if e["name"] == "broken_link"), None)
    assert broken_entry is not None, "broken symlink should still be listed"
    assert broken_entry["type"] == "link"
    # 不再读取 stat 元数据，损坏符号链接也不会触发 OSError 兜底，整体列举保持成功。
    assert "size" not in broken_entry and "modified" not in broken_entry


def test_offset_out_of_range_is_explicit(populated: Path, context) -> None:
    """offset 越界给出明确提示，不静默伪装成空目录（#4）。"""
    result = ListDirectoryTool().execute(
        path=".", execution_context=context, offset=10_000, limit=50
    )
    assert result.status == "success"
    assert (
        "no entries at offset=" in result.content
    ), f"expected explicit out-of-range hint, got: {result.content!r}"
    assert "offset=10000" in result.content


def test_empty_directory_reports_empty(populated: Path, context) -> None:
    """真空目录返回 (empty directory) 且无越界提示（与越界区分）。"""
    empty = populated / "empty_dir"
    empty.mkdir()
    result = ListDirectoryTool().execute(path="empty_dir", execution_context=context)
    assert result.status == "success"
    assert result.content == "(empty directory)"
    assert "offset=" not in result.content


def test_normal_listing_contains_expected_entries(populated: Path, context) -> None:
    """正常列举返回排序后的条目，目录在前（dir 类型）。"""
    result = ListDirectoryTool().execute(path=".", execution_context=context)
    assert result.status == "success"
    entries = (result.data or {}).get("entries", [])
    names = {e["name"] for e in entries}
    assert {"src", "top.py"}.issubset(names)
    src = next(e for e in entries if e["name"] == "src")
    assert src["type"] == "dir"


def test_scandir_oserror_returns_structured_error(
    populated: Path, context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """列举时若 scandir 抛 OSError（如 TOCTOU 竞态下目录被删/权限撤销），必须归一化为
    结构化 tool_error，而非让异常逃逸工具调度层（发现 7）。"""
    original_scandir = os.scandir

    def _boom(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("directory vanished between pre-check and read")

    monkeypatch.setattr(os, "scandir", _boom)
    try:
        result = ListDirectoryTool().execute(path=".", execution_context=context)
    finally:
        monkeypatch.setattr(os, "scandir", original_scandir)
    assert (
        result.status == "error"
    ), f"scandir OSError should be normalized to tool_error, got {result.status!r}"
    assert (
        result.error and "vanished" in result.error
    ), f"error should carry the underlying cause, got {result.error!r}"
    assert result.reason, "structured reason must guide the model how to recover"
