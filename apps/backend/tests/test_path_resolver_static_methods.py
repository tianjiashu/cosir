"""PathResolver 三个新增静态方法（is_inside_workspace / escapes_workspace / with_workspace_ancestors）的单元测试。

聚焦本次迁移重构从 ``file_resource_paths`` 私有函数提取为 ``PathResolver`` 静态方法的
行为契约：
- ``is_inside_workspace``：强 containment 判定（跟随符号链接）；workspace 内嵌套、根自身为真；
  越界/同级/无法判定为假（保守判越界）。
- ``escapes_workspace``：``is_inside_workspace`` 的取反；越界为真、workspace 内为假。
- ``with_workspace_ancestors``：为写路径补充根以下的祖先目录锁；根自身不纳入；
  去重；越界 path 经 ``is_relative_to`` 守卫跳过。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.tools.tool_handler.security.path_resolver import PathResolver


def _workspace_root(tmp_path: Path) -> Path:
    """以临时目录为 workspace 根，返回 resolve 后的规范绝对路径（与实现同基准）。"""
    return tmp_path.resolve(strict=False)


# ---------------------------------------------------------------------------
# is_inside_workspace
# ---------------------------------------------------------------------------

def test_is_inside_workspace_nested_file(tmp_path: Path) -> None:
    """workspace 内嵌套路径应判定为 True（强 containment 命中）。"""
    root = _workspace_root(tmp_path)
    nested = root / "a" / "b" / "file.txt"
    assert PathResolver.is_inside_workspace(root, nested) is True


def test_is_inside_workspace_root_itself(tmp_path: Path) -> None:
    """workspace 根自身应判定为 True（relative_to 允许相同路径）。"""
    root = _workspace_root(tmp_path)
    assert PathResolver.is_inside_workspace(root, root) is True


def test_is_inside_workspace_out_of_bounds_sibling(tmp_path: Path) -> None:
    """workspace 同级（兄弟目录）路径应判定为 False（越界）。"""
    root = _workspace_root(tmp_path)
    outside = tmp_path.parent / "outside_workspace" / "evil.txt"
    assert PathResolver.is_inside_workspace(root, outside) is False


def test_is_inside_workspace_out_of_bounds_relative(tmp_path: Path) -> None:
    """词法上带 ``..`` 逃逸 workspace 的路径应判定为 False（越界，保守拒绝）。"""
    root = _workspace_root(tmp_path)
    escaping = root / ".." / "sibling" / "x.txt"
    assert PathResolver.is_inside_workspace(root, escaping) is False


@pytest.mark.skipif(
    not hasattr(Path, "symlink_to") or __import__("os").name == "nt",
    reason="符号链接创建需特权/该平台不支持",
)
def test_is_inside_workspace_follows_symlink(tmp_path: Path) -> None:
    """指向 workspace 外的符号链接应判定为 False（跟随链接强 containment，防借链接逃逸）。"""
    root = _workspace_root(tmp_path)
    outside = tmp_path.parent / "outside_target"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "link_out"
    link.symlink_to(outside, target_is_directory=True)
    # 词法在 workspace 内，但 resolve 后指向 workspace 外 → 判 False。
    assert PathResolver.is_inside_workspace(root, link) is False


# ---------------------------------------------------------------------------
# escapes_workspace
# ---------------------------------------------------------------------------

def test_escapes_workspace_true_for_out_of_bounds(tmp_path: Path) -> None:
    """越界路径应判定为 escapes=True（取反于 is_inside_workspace）。"""
    root = _workspace_root(tmp_path)
    outside = tmp_path.parent / "outside_workspace" / "x.txt"
    assert PathResolver.escapes_workspace(root, outside) is True


def test_escapes_workspace_false_for_inside(tmp_path: Path) -> None:
    """workspace 内路径应判定为 escapes=False。"""
    root = _workspace_root(tmp_path)
    inside = root / "src" / "main.py"
    assert PathResolver.escapes_workspace(root, inside) is False


# ---------------------------------------------------------------------------
# with_workspace_ancestors
# ---------------------------------------------------------------------------

def test_with_workspace_ancestors_nested(tmp_path: Path) -> None:
    """嵌套写路径应补充各级祖先目录锁，且不含 workspace 根自身。"""
    root = _workspace_root(tmp_path)
    nested = root / "a" / "b" / "c.txt"
    result = PathResolver.with_workspace_ancestors(root, (nested,))
    assert set(result) == {
        root / "a" / "b" / "c.txt",
        root / "a" / "b",
        root / "a",
    }
    assert root not in result  # 根自身不纳入


def test_with_workspace_ancestors_dedupes(tmp_path: Path) -> None:
    """多个共享祖先的写路径应去重（同一祖先目录只出现一次）。"""
    root = _workspace_root(tmp_path)
    a = root / "a" / "x.txt"
    b = root / "a" / "y.txt"
    result = PathResolver.with_workspace_ancestors(root, (a, b))
    assert result.count(root / "a") == 1  # 共享祖先去重


def test_with_workspace_ancestors_empty_input(tmp_path: Path) -> None:
    """空输入应返回空元组。"""
    root = _workspace_root(tmp_path)
    assert PathResolver.with_workspace_ancestors(root, ()) == ()


def test_with_workspace_ancestors_root_level_path(tmp_path: Path) -> None:
    """workspace 根一级的路径应仅含自身（无祖先，根不纳入）。"""
    root = _workspace_root(tmp_path)
    direct = root / "top.txt"
    result = PathResolver.with_workspace_ancestors(root, (direct,))
    assert result == (direct,)


def test_with_workspace_ancestors_out_of_bounds_guard(tmp_path: Path) -> None:
    """越界写路径仅保留其自身，不补充祖先锁键（防越界祖先锁泄漏）。

    迁移前后等价：path 自身始终保留，但 ``is_relative_to`` / ``relative_to`` 守卫
    阻止追加越界祖先，使锁键不越过 workspace 根向上扩张。
    """
    root = _workspace_root(tmp_path)
    outside = tmp_path.parent / "outside_workspace" / "evil.txt"
    result = PathResolver.with_workspace_ancestors(root, (outside,))
    # 仅保留越界 path 自身，无任何祖先被补充。
    assert result == (outside,)
    # 越界路径的祖先（workspace 外目录）不应出现在结果中。
    assert outside.parent not in result
    assert root not in result


def test_with_workspace_ancestors_multiple_separate_branches(tmp_path: Path) -> None:
    """不同分支的写路径应各自补全各自祖先，且互不混淆。"""
    root = _workspace_root(tmp_path)
    p1 = root / "x" / "1.txt"
    p2 = root / "y" / "2.txt"
    result = set(PathResolver.with_workspace_ancestors(root, (p1, p2)))
    assert result == {
        root / "x" / "1.txt",
        root / "x",
        root / "y" / "2.txt",
        root / "y",
    }


# ---------------------------------------------------------------------------
# 迁移相关 PathResolver 既有方法补充（输入校验与设备拦截分支）
# ---------------------------------------------------------------------------

def test_resolve_within_workspace_rejects_empty_path(tmp_path: Path) -> None:
    """resolve_within_workspace 对空路径返回错误（_validate_path 分支）。"""
    resolver = PathResolver(tmp_path)
    resolved, error = resolver.resolve_within_workspace("")
    assert resolved is None
    assert "non-empty string" in error


def test_resolve_within_workspace_rejects_nul(tmp_path: Path) -> None:
    """resolve_within_workspace 对含 NUL 路径返回错误。"""
    resolver = PathResolver(tmp_path)
    resolved, error = resolver.resolve_within_workspace("a\x00b.txt")
    assert resolved is None
    assert "NUL" in error


def test_resolve_entry_within_workspace_rejects_empty_path(tmp_path: Path) -> None:
    """resolve_entry_within_workspace 对空路径返回错误。"""
    resolver = PathResolver(tmp_path)
    resolved, error = resolver.resolve_entry_within_workspace("")
    assert resolved is None
    assert "non-empty string" in error


def test_resolve_without_boundary_rejects_nul(tmp_path: Path) -> None:
    """resolve_without_boundary 对含 NUL 路径返回错误。"""
    resolver = PathResolver(tmp_path)
    resolved, error = resolver.resolve_without_boundary("a\x00b")
    assert resolved is None
    assert "NUL" in error


def test_blocked_device_reason_posix_blocked(tmp_path: Path) -> None:
    """POSIX 设备路径（/dev/null）应返回非空拦截原因。"""
    resolver = PathResolver(tmp_path)
    reason = resolver.blocked_device_reason("/dev/null")
    assert reason
    assert "blocked device path" in reason


def test_blocked_device_reason_resolved_target_blocked(tmp_path: Path) -> None:
    """符号链接解析后指向设备路径应被拦截（第二层安检）。"""
    resolver = PathResolver(tmp_path)
    reason = resolver.blocked_device_reason("link_to_dev", resolved=Path("/dev/urandom"))
    assert reason
    assert "resolves to" in reason


def test_blocked_device_reason_regular_path_allowed(tmp_path: Path) -> None:
    """普通路径不应被拦截（返回空串表示允许）。"""
    resolver = PathResolver(tmp_path)
    assert resolver.blocked_device_reason(str(tmp_path / "normal.txt")) == ""


def test_blocked_recursive_search_reason_blocks_sys(tmp_path: Path) -> None:
    """递归搜索 /sys 树应被拒绝（敏感伪文件树）。"""
    resolver = PathResolver(tmp_path)
    reason = resolver.blocked_recursive_search_reason("/sys")
    assert reason
    assert "blocked recursive search" in reason
