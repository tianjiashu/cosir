"""PathResolver 路径安全解析器的严苛单元测试。

PathResolver 是 write_file / patch 两个工具的 containment 唯一收口，安全相关，
逐分支验证：设备名拦截、越界拦截、三种解析策略、NUL/空串输入校验、
递归搜索拦截、以及供上游复用的静态方法。
"""

from pathlib import Path

import pytest

from app.tools.tool_handler.security.path_resolver import PathResolver


@pytest.fixture
def resolver(tmp_path: Path) -> PathResolver:
    return PathResolver(tmp_path)


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #


def test_empty_path_rejected(resolver):
    resolved, err = resolver.resolve_within_workspace("")
    assert resolved is None
    assert "non-empty" in err


def test_whitespace_path_rejected(resolver):
    resolved, err = resolver.resolve_within_workspace("   ")
    assert resolved is None


def test_nul_char_rejected(resolver):
    resolved, err = resolver.resolve_within_workspace("a\x00b")
    assert resolved is None
    assert "NUL" in err


def test_non_string_rejected():
    # 直接调用静态校验，验证非字符串被拒。
    assert PathResolver._validate_path(123) != ""
    assert PathResolver._validate_path(None) != ""


# --------------------------------------------------------------------------- #
# resolve_within_workspace containment
# --------------------------------------------------------------------------- #


def test_relative_path_resolved_inside(resolver, tmp_path):
    resolved, err = resolver.resolve_within_workspace("sub/file.txt")
    assert err == ""
    assert resolved == (tmp_path / "sub" / "file.txt").resolve()


def test_parent_escape_rejected(resolver):
    resolved, err = resolver.resolve_within_workspace("../escape.txt")
    assert resolved is None
    assert "escapes project root" in err


def test_absolute_inside_ok(resolver, tmp_path):
    target = tmp_path / "inside.txt"
    resolved, err = resolver.resolve_within_workspace(str(target))
    assert err == ""


# --------------------------------------------------------------------------- #
# resolve_without_boundary（只读工具允许越界）
# --------------------------------------------------------------------------- #


def test_without_boundary_allows_outside(resolver, tmp_path):
    outside = tmp_path.parent / "outside.txt"
    resolved, err = resolver.resolve_without_boundary(str(outside))
    assert err == ""
    assert resolved == outside.resolve()


def test_without_boundary_rejects_empty(resolver):
    resolved, err = resolver.resolve_without_boundary("")
    assert resolved is None


# --------------------------------------------------------------------------- #
# resolve_entry_within_workspace（不跟随末级符号链接）
# --------------------------------------------------------------------------- #


def test_resolve_entry_inside(resolver, tmp_path):
    entry, err = resolver.resolve_entry_within_workspace("sub/link")
    assert err == ""
    assert entry == (tmp_path / "sub" / "link")


def test_resolve_entry_escape_rejected(resolver):
    entry, err = resolver.resolve_entry_within_workspace("../x")
    assert entry is None


# --------------------------------------------------------------------------- #
# 设备名 / POSIX 敏感路径拦截
# --------------------------------------------------------------------------- #


def test_windows_device_names_blocked(resolver):
    for name in ("NUL", "con", "COM1", "LPT1", "aux.txt", "PRN.log"):
        assert resolver.blocked_device_reason(name) != "", name


def test_regular_name_allowed(resolver):
    assert resolver.blocked_device_reason("normal.txt") == ""
    assert resolver.blocked_device_reason("console.txt") == ""  # 非精确设备名


def test_posix_device_paths_blocked(resolver):
    assert resolver.blocked_device_reason("/dev/null") != ""
    assert resolver.blocked_device_reason("/proc/self/environ") != ""


def test_blocked_device_reason_on_resolved(resolver):
    """第二层：解析后的真实目标命中设备名也被拦截。"""

    reason = resolver.blocked_device_reason("weird", Path("/dev/zero"))
    assert reason != ""


# --------------------------------------------------------------------------- #
# 递归搜索拦截
# --------------------------------------------------------------------------- #


def test_blocked_recursive_search_roots(resolver):
    assert resolver.blocked_recursive_search_reason("/proc") != ""
    assert resolver.blocked_recursive_search_reason("/sys/kernel") != ""


def test_recursive_search_normal_allowed(resolver):
    assert resolver.blocked_recursive_search_reason("src") == ""


# --------------------------------------------------------------------------- #
# 静态工具方法
# --------------------------------------------------------------------------- #


def test_is_inside_workspace(tmp_path):
    inside = tmp_path / "a" / "b.txt"
    assert PathResolver.is_inside_workspace(tmp_path, inside) is True


def test_escapes_workspace(tmp_path):
    outside = tmp_path.parent / "z.txt"
    assert PathResolver.escapes_workspace(tmp_path, outside) is True


def test_with_workspace_ancestors_excludes_root(tmp_path):
    target = tmp_path / "a" / "b" / "c.txt"
    result = PathResolver.with_workspace_ancestors(tmp_path, (target,))
    # 含目标与其祖先目录，但不含 workspace 根自身。
    assert target in result
    assert (tmp_path / "a") in result
    assert (tmp_path / "a" / "b") in result
    assert tmp_path.resolve() not in result


def test_with_workspace_ancestors_skips_outside(tmp_path):
    outside = tmp_path.parent / "out.txt"
    result = PathResolver.with_workspace_ancestors(tmp_path, (outside,))
    # 越界 path 仍纳入自身但不展开祖先（is_relative_to 守卫）。
    assert outside in result
