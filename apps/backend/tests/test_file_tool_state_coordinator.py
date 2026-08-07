"""FileToolStateCoordinator 的单元回归测试。

聚焦本次「统一执行管线」改动的实质核心：
- 非文件工具（resource_keys 不含 ``"filesystem"``）经 ``prepare`` 短路返回空计划；
- 空计划在 ``lock`` / ``check_stale`` / ``complete`` 下全部 no-op（不抛、不写 registry）。
- 文件工具路径不受影响，仍产出含资源的执行计划。

并覆盖「模型输入路径不可信」场景的纵深防御：
- 越界路径（workspace 外绝对/相对）在 prepare 阶段即被拒绝，不泄漏哨兵、不纳入锁；
- 空 / None path 直接报错；
- delete 的 prepare 解析与 handler（PathResolver.resolve_entry）同源；
- 重复调用签名对等价路径写法（相对 / 绝对 / 双斜杠）以 workspace 根为基准归一一致；
- search_files 指定越界根时不触发全量目录遍历（防 DoS）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.tools.guard.file_resource_paths import FileResourcePathError, FileResourcePaths
from app.tools.guard.file_tool_state_coordinator import (
    FileToolStateCoordinator,
    normalize_repeated_call_arguments,
)
from app.tools.schemas import ToolDefinition, ToolExecutionContext, ToolObservation
from app.tools.tool_handler.security.path_resolver import PathResolver


class _NoArgs(BaseModel):
    """无字段参数模型，仅用于构造 ToolDefinition。"""


def _make_context(tmp_path: Path) -> ToolExecutionContext:
    """构造一个最小可用的执行上下文，以临时目录作为 workspace 根。"""
    return ToolExecutionContext(
        task_id="task-1",
        workspace_id="ws-1",
        workspace_root=tmp_path,
    )


def _make_tool(*, resource_keys: tuple[str, ...], name: str = "write_file") -> ToolDefinition:
    """构造一个最小工具定义，仅用于协调器 prepare 路径分发。"""
    return ToolDefinition(
        name=name,
        description="test tool",
        permission="file_write",
        handler=lambda *a, **k: None,
        args_model=_NoArgs,
        resource_keys=resource_keys,
    )


def _outside_path(tmp_path: Path) -> Path:
    """构造一个跨平台的 workspace 外路径：``tmp_path`` 的同级 ``outside`` 目录。

    不硬编码盘符，避免在非 Windows CI 上被解析成 workspace 内相对路径。
    """
    return tmp_path.parent / "outside_workspace"


def test_prepare_short_circuits_for_non_filesystem_tool(tmp_path: Path) -> None:
    """非文件工具应短路返回空计划，且之后各协调方法全部 no-op。"""
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("shell",), name="execute_terminal")
    ctx = _make_context(tmp_path)

    plan = coordinator.prepare(tool, {}, ctx, tool_call_id="call-1")

    # 空计划：无资源、无观察路径、无 early_observation。
    assert plan.resources == FileResourcePaths()
    assert plan.observed_paths == ()
    assert plan.early_observation is None

    with coordinator.lock(plan, ctx):
        pass
    assert coordinator.check_stale(plan, tool, ctx, tool_call_id="call-1") is None
    coordinator.complete(plan, _success_observation(), ctx)


def test_prepare_does_not_short_circuit_for_filesystem_tool(tmp_path: Path) -> None:
    """文件工具路径不受影响，prepare 对 filesystem 工具正常执行不短路（对照用例）。"""
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("filesystem",))
    ctx = _make_context(tmp_path)
    expected = (tmp_path / "foo.txt").resolve()

    plan = coordinator.prepare(
        tool,
        {"path": str(tmp_path / "foo.txt"), "content": "x"},
        ctx,
        tool_call_id="call-2",
    )

    # 真实断言：写/锁键指向 workspace 内规范路径，且 lock_paths 含该目标。
    assert plan.resources.write_paths == (expected,)
    assert expected in plan.resources.lock_paths


def test_write_file_rejects_out_of_bounds_relative_path(tmp_path: Path) -> None:
    """越界相对路径在 prepare 阶段即被拒绝（纵深防御，不泄漏哨兵）。"""
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("filesystem",))
    ctx = _make_context(tmp_path)

    with pytest.raises(FileResourcePathError) as exc_info:
        coordinator.prepare(
            tool,
            {"path": "../outside.txt", "content": "x"},
            ctx,
            tool_call_id="call-3",
        )
    assert exc_info.value.reason  # reason 为富文本，非空
    assert "always be rejected" in exc_info.value.reason


def test_write_file_rejects_out_of_bounds_absolute_path(tmp_path: Path) -> None:
    """越界绝对路径（workspace 外目录）在 prepare 阶段即被拒绝（跨平台）。"""
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("filesystem",))
    ctx = _make_context(tmp_path)
    outside = _outside_path(tmp_path) / "evil.txt"

    with pytest.raises(FileResourcePathError):
        coordinator.prepare(
            tool,
            {"path": str(outside), "content": "x"},
            ctx,
            tool_call_id="call-4",
        )


def test_write_file_rejects_empty_path(tmp_path: Path) -> None:
    """空 / None path 直接报错，不生成 workspace 外哨兵。"""
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("filesystem",))
    ctx = _make_context(tmp_path)

    with pytest.raises(FileResourcePathError):
        coordinator.prepare(tool, {"path": "", "content": "x"}, ctx, tool_call_id="call-5")
    with pytest.raises(FileResourcePathError):
        coordinator.prepare(tool, {"path": None, "content": "x"}, ctx, tool_call_id="call-6")


def test_delete_resolves_same_as_handler_entry(tmp_path: Path) -> None:
    """delete 的 prepare 写/锁键与 handler（PathResolver.resolve_entry）同源，不跟随末级链接。"""
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("filesystem",), name="delete")
    ctx = _make_context(tmp_path)
    target = tmp_path / "to_delete.txt"
    target.write_text("data", encoding="utf-8")

    plan = coordinator.prepare(tool, {"path": "to_delete.txt"}, ctx, tool_call_id="call-7")
    handler_entry, _ = PathResolver(tmp_path).resolve_entry_within_workspace("to_delete.txt")
    assert plan.resources.write_paths == (handler_entry,)
    assert handler_entry in plan.resources.lock_paths


@pytest.mark.skipif(
    not hasattr(os, "symlink") or os.name == "nt",
    reason="symbolic link creation requires privilege/unsupported on this platform",
)
def test_delete_symlink_to_outside_uses_entry_not_target(tmp_path: Path) -> None:
    """指向 workspace 外的符号链接：prepare 锁键应是链接自身，而非目标，且不越界。

    验证缺陷2 修复的"删链接自身 vs 删链接目标"键一致：即便链接指向 workspace 外，
    resolve_entry 仍允许删除链接项，prepare 不应抛 FileResourcePathError；所有锁键必须
    落在 workspace 内且不含 workspace 根自身，写键指向链接自身（末级保持词法）。
    """
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("filesystem",), name="delete")
    ctx = _make_context(tmp_path)
    outside = _outside_path(tmp_path)
    link = tmp_path / "link_to_outside"
    link.symlink_to(outside, target_is_directory=True)
    workspace_root = tmp_path.resolve(strict=False)

    plan = coordinator.prepare(tool, {"path": "link_to_outside"}, ctx, tool_call_id="call-7c")
    entry, _ = PathResolver(tmp_path).resolve_entry_within_workspace("link_to_outside")
    # 核心：所有锁键落在 workspace 内，且不含 workspace 根自身。
    for lock_path in plan.resources.lock_paths:
        assert lock_path.is_relative_to(workspace_root), f"锁键越界: {lock_path}"
        assert lock_path != workspace_root, "workspace 根不应纳入锁键"
    # 未跟随末级链接：写键是链接自身而非其 workspace 外目标。
    assert plan.resources.write_paths == (entry,)
    assert not plan.resources.write_paths[0].resolve(strict=False).is_relative_to(workspace_root)


def test_write_file_locks_nested_ancestors_excluding_root(tmp_path: Path) -> None:
    """嵌套写路径应锁定各级祖先目录，但不含 workspace 根自身、不上溯越界。"""
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("filesystem",))
    ctx = _make_context(tmp_path)
    nested = tmp_path / "a" / "b" / "c.txt"
    nested.parent.mkdir(parents=True)
    root = tmp_path.resolve(strict=False)

    plan = coordinator.prepare(
        tool, {"path": "a/b/c.txt", "content": "x"}, ctx, tool_call_id="call-11"
    )

    assert set(plan.resources.lock_paths) == {
        root / "a" / "b" / "c.txt",
        root / "a" / "b",
        root / "a",
    }
    assert root not in plan.resources.lock_paths  # 根不纳入
    assert tmp_path.parent not in plan.resources.lock_paths  # 不上溯越界


def test_delete_rejects_out_of_bounds_path(tmp_path: Path) -> None:
    """delete 越界路径在 prepare 阶段即被拒绝，与 handler resolve_entry 同源。"""
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("filesystem",), name="delete")
    ctx = _make_context(tmp_path)
    outside = _outside_path(tmp_path) / "evil.txt"

    with pytest.raises(FileResourcePathError):
        coordinator.prepare(
            tool,
            {"path": str(outside)},
            ctx,
            tool_call_id="call-7b",
        )


def test_patch_empty_path_no_write_resource(tmp_path: Path) -> None:
    """patch replace 模式缺 path 时返回空 write_paths，而非越界哨兵。"""
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("filesystem",), name="patch")
    ctx = _make_context(tmp_path)

    plan = coordinator.prepare(
        tool,
        {"mode": "replace", "content": "x"},
        ctx,
        tool_call_id="call-8",
    )
    assert plan.resources.write_paths == ()


def test_repeated_signature_normalizes_equivalent_paths(tmp_path: Path) -> None:
    """等价路径的不同写法（相对/绝对/双斜杠）应以 workspace 根为基准产生相同签名。"""
    abs_p = normalize_repeated_call_arguments(
        "read_file", {"path": str(tmp_path / "real" / "f.txt")}, tmp_path
    )
    dbl = normalize_repeated_call_arguments(
        "read_file", {"path": str(tmp_path / "real//f.txt")}, tmp_path
    )
    rel = normalize_repeated_call_arguments("read_file", {"path": "real/f.txt"}, tmp_path)
    assert abs_p == dbl == rel


def test_normalize_patch_replace_mode_keeps_path_field(tmp_path: Path) -> None:
    """patch replace 模式应保留 mode 并归一 path 字段。"""
    normalized = normalize_repeated_call_arguments(
        "patch", {"mode": "replace", "path": "a/b.txt", "content": "x"}, tmp_path
    )
    assert normalized["mode"] == "replace"
    assert normalized["path"] == os.path.normcase(os.path.abspath(str(tmp_path / "a/b.txt")))


def test_normalize_patch_v4a_mode_keeps_patch_text(tmp_path: Path) -> None:
    """patch V4A 模式应保留 mode 并原样保留 patch 文本（非路径字段不归一）。"""
    patch_text = "*** Update File: a.txt\n@@\n-x\n+y"
    normalized = normalize_repeated_call_arguments(
        "patch", {"mode": "v4a", "patch": patch_text}, tmp_path
    )
    assert normalized == {"mode": "v4a", "patch": patch_text}


def test_normalize_non_string_path_passthrough(tmp_path: Path) -> None:
    """非字符串 path 值应原样返回，不污染非路径字段（None / 数字等）。"""
    normalized = normalize_repeated_call_arguments("read_file", {"path": None}, tmp_path)
    assert normalized == {"path": None, "offset": None, "limit": None}
    normalized_num = normalize_repeated_call_arguments("read_file", {"path": 42}, tmp_path)
    assert normalized_num == {"path": 42, "offset": None, "limit": None}


def test_search_files_out_of_bounds_scope_skips_traversal(tmp_path: Path) -> None:
    """search_files 指定越界根时不遍历目录，避免 DoS；scope_escapes_workspace=True。"""
    coordinator = FileToolStateCoordinator(max_scope_paths=10)
    tool = _make_tool(resource_keys=("filesystem",), name="search_files")
    ctx = _make_context(tmp_path)
    outside = _outside_path(tmp_path)

    plan = coordinator.prepare(
        tool,
        {"path": str(outside), "pattern": "*.dll"},
        ctx,
        tool_call_id="call-9",
    )
    assert plan.resources.scope_escapes_workspace is True
    # 仅保留 scope 根自身，不展开遍历（observed_paths 不随越界目录膨胀）。
    assert plan.observed_paths == (outside.resolve(strict=False),)
    assert plan.snapshot_complete is True


def test_read_file_out_of_bounds_allowed_but_not_for_lock(tmp_path: Path) -> None:
    """read_file 越界路径允许只读，但不进 lock/write（只读工具无写资源）。"""
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("filesystem",), name="read_file")
    ctx = _make_context(tmp_path)
    outside = _outside_path(tmp_path) / "notepad.exe"

    plan = coordinator.prepare(
        tool,
        {"path": str(outside)},
        ctx,
        tool_call_id="call-10",
    )
    assert plan.resources.write_paths == ()
    assert plan.resources.lock_paths == ()


def _success_observation() -> ToolObservation:
    """构造一个成功态 ToolObservation，供 complete no-op 验证。"""
    return ToolObservation(tool_name="execute_terminal", status="success", content="ok", data={})
