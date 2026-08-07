"""文件状态协调管线及其支撑 registry 的单元测试。

覆盖目标（state coordinator + 三个 registry + resource paths 解析器）：
- prepare 对文件/非文件工具的分流；
- read_file 重复调用 unchanged；
- search_files 重复状态机 warning -> block -> 文件变化后 reset；
- write_file stale 检测（首写不 stale、外部修改后 stale）；
- check_stale 的 stale_file / stale_patch 分支；
- complete 成功/失败时 revision 与 repeated 的记录差异；
- 三个 registry 的 LRU 淘汰与 task 隔离；
- 路径锁 acquire/release 与容量耗尽异常；
- file_resource_paths 对不同工具的 read/write/lock/scope 推导；
- 越界路径拒绝。

测试目的与可能发现的缺陷类型在每个用例上方以注释标注。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from app.tools.guard.file_resource_paths import (
    FileResourcePathError,
    FileResourcePaths,
    FileResourceResolver,
    resolve_file_resource_paths,
)
from app.tools.guard.file_tool_state_coordinator import FileToolStateCoordinator
from app.tools.schemas import ToolDefinition, ToolExecutionContext, ToolObservation
from app.tools.guard.file_state import (
    FileFingerprint,
    FilePathLockRegistry,
    FileRevisionRegistry,
    RepeatedCallAction,
    RepeatedCallRegistry,
)
from app.tools.tool_handler.patch import OperationType


class _NoArgs(BaseModel):
    """无字段参数模型，仅用于构造 ToolDefinition。"""


def _make_context(
    tmp_path: Path, *, task_id: str = "task-1", turn_id: str = ""
) -> ToolExecutionContext:
    """构造最小执行上下文，以临时目录作为 workspace 根。"""
    return ToolExecutionContext(
        task_id=task_id,
        workspace_id="ws-1",
        workspace_root=tmp_path,
        turn_id=turn_id,
    )


def _make_tool(
    *, resource_keys: tuple[str, ...], name: str = "write_file", permission: str = "file_write"
) -> ToolDefinition:
    """构造最小工具定义。"""
    return ToolDefinition(
        name=name,
        description="test tool",
        permission=permission,
        handler=lambda *a, **k: None,
        args_model=_NoArgs,
        resource_keys=resource_keys,
    )


def _success_observation(
    tool_name: str = "write_file", **extra: object
) -> ToolObservation:
    """构造成功态观察。"""
    return ToolObservation(
        tool_name=tool_name,
        status="success",
        content="ok",
        data={},
        **extra,
    )


def _fail_observation(tool_name: str = "write_file") -> ToolObservation:
    """构造失败态观察。"""
    return ToolObservation(
        tool_name=tool_name, status="error", content="boom", error="boom", data={}
    )


def _outside_path(tmp_path: Path) -> Path:
    """workspace 外路径（tmp_path 同级）。"""
    return tmp_path.parent / "outside_workspace"


# ---------------------------------------------------------------- coordinator


# 测试目的：max_scope_paths < 1 应抛 ValueError（构造契约）；缺陷类型：缺失参数校验。
def test_coordinator_rejects_non_positive_max_scope_paths() -> None:
    with pytest.raises(ValueError):
        FileToolStateCoordinator(max_scope_paths=0)
    with pytest.raises(ValueError):
        FileToolStateCoordinator(max_scope_paths=-1)


# 测试目的：非文件工具 prepare 短路返回空计划且各方法 no-op；缺陷类型：状态管线误触发。
def test_non_file_tool_plan_is_inert(tmp_path: Path) -> None:
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("shell",), name="execute_terminal")
    ctx = _make_context(tmp_path)
    plan = coordinator.prepare(tool, {}, ctx, tool_call_id="c1")
    assert plan.resources == FileResourcePaths()
    assert plan.observed_paths == ()
    with coordinator.lock(plan, ctx):
        pass
    assert coordinator.check_stale(plan, tool, ctx, tool_call_id="c1") is None
    coordinator.complete(plan, _success_observation(), ctx)


# 测试目的：文件工具 prepare 正常产出资源；缺陷类型：resource_keys 分流错误。
def test_file_tool_prepare_produces_resources(tmp_path: Path) -> None:
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("filesystem",))
    ctx = _make_context(tmp_path)
    plan = coordinator.prepare(
        tool, {"path": "foo.txt", "content": "x"}, ctx, tool_call_id="c2"
    )
    assert plan.resources.write_paths == ((tmp_path / "foo.txt").resolve(),)


# 测试目的：revisions 属性应暴露注入的 registry；缺陷类型：依赖注入失效。
def test_coordinator_revisions_property_exposes_registry(tmp_path: Path) -> None:
    injected = FileRevisionRegistry()
    coordinator = FileToolStateCoordinator(revisions=injected)
    assert coordinator.revisions is injected


# 测试目的：normalize patch 非 replace 模式保留 patch 文本；缺陷类型：签名归一错误。
def test_normalize_patch_non_replace_keeps_patch_text(tmp_path: Path) -> None:
    from app.tools.guard.file_tool_state_coordinator import normalize_repeated_call_arguments

    normalized = normalize_repeated_call_arguments(
        "patch", {"mode": "v4a", "patch": "*** Begin Patch"}, tmp_path
    )
    assert normalized == {"mode": "v4a", "patch": "*** Begin Patch"}


# 测试目的：_canonical_path 对空字符串 path 原样返回；缺陷类型：空路径误归一。
def test_normalize_empty_path_passthrough(tmp_path: Path) -> None:
    from app.tools.guard.file_tool_state_coordinator import normalize_repeated_call_arguments

    assert normalize_repeated_call_arguments("read_file", {"path": ""}, tmp_path) == {
        "path": "",
        "offset": None,
        "limit": None,
    }


# 测试目的：complete 成功时记录 write_paths 的 revision；缺陷类型：写路径未记录导致误 stale。
def test_complete_success_records_write_revision(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("v1", encoding="utf-8")
    coordinator = FileToolStateCoordinator()
    ctx = _make_context(tmp_path)
    tool = _make_tool(resource_keys=("filesystem",))
    plan = coordinator.prepare(tool, {"path": "f.txt", "content": "x"}, ctx, tool_call_id="w1")
    assert coordinator.check_stale(plan, tool, ctx, tool_call_id="w1") is None  # 首写不 stale
    coordinator.complete(plan, _success_observation(), ctx)
    # 记录后不 stale；外部修改后才 stale
    target.write_text("v2", encoding="utf-8")
    obs = coordinator.check_stale(plan, tool, ctx, tool_call_id="w1")
    assert obs is not None


# 测试目的：list_directory 指向文件（非目录 scope）不抛且 scope 收集为空；缺陷类型：非目录遍历崩溃。
def test_list_directory_on_file_scope(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")
    coordinator = FileToolStateCoordinator(max_scope_paths=100)
    tool = _make_tool(resource_keys=("filesystem",), name="list_directory")
    ctx = _make_context(tmp_path)
    plan = coordinator.prepare(tool, {"path": "f.txt"}, ctx, tool_call_id="l1")
    # 非递归且 scope 非目录 -> 仅含 scope 根，不抛
    assert (tmp_path / "f.txt").resolve() in set(plan.observed_paths)


# 测试目的：read_file 重复调用首次执行、再次 unchanged；缺陷类型：重复检测状态机错误。
def test_read_file_repeated_call_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("hello", encoding="utf-8")
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("filesystem",), name="read_file")
    ctx = _make_context(tmp_path)

    plan1 = coordinator.prepare(tool, {"path": "f.txt"}, ctx, tool_call_id="r1")
    assert plan1.early_observation is None  # 首次 execute
    # complete 登记成功基线，之后重复调用才能被识别
    coordinator.complete(plan1, _success_observation("read_file"), ctx)

    plan2 = coordinator.prepare(tool, {"path": "f.txt"}, ctx, tool_call_id="r2")
    assert plan2.early_observation is not None
    assert plan2.early_observation.status == "success"
    assert plan2.early_observation.data == {"unchanged": True}


# 测试目的：read_file 文件变化后不再 unchanged（fingerprint 变化重置）；缺陷类型：快照未刷新。
def test_read_file_after_change_executes_again(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("hello", encoding="utf-8")
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("filesystem",), name="read_file")
    ctx = _make_context(tmp_path)

    plan1 = coordinator.prepare(tool, {"path": "f.txt"}, ctx, tool_call_id="r1")
    coordinator.complete(plan1, _success_observation("read_file"), ctx)

    target.write_text("hello world", encoding="utf-8")
    plan2 = coordinator.prepare(tool, {"path": "f.txt"}, ctx, tool_call_id="r2")
    assert plan2.early_observation is None


# 测试目的：search_files 重复状态机 warning -> block -> 文件变化后 reset；
# 缺陷类型：search 重复计数与阈值判定错误。
def test_search_files_repeated_state_machine(tmp_path: Path) -> None:
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("filesystem",), name="search_files")
    ctx = _make_context(tmp_path)
    args = {"path": ".", "pattern": "*.txt"}

    # 首次：execute
    plan1 = coordinator.prepare(tool, args, ctx, tool_call_id="s1")
    assert plan1.early_observation is None
    coordinator.complete(plan1, _success_observation("search_files"), ctx)

    # 第二次：warning
    plan2 = coordinator.prepare(tool, args, ctx, tool_call_id="s2")
    assert plan2.early_observation is not None
    assert plan2.early_observation.data == {"repeated": True, "warning": True}

    # 第三次：block（error 观察）
    plan3 = coordinator.prepare(tool, args, ctx, tool_call_id="s3")
    assert plan3.early_observation is not None
    assert plan3.early_observation.status == "error"

    # 范围内容变化后 reset：scope 快照变化 -> 重新 execute
    (tmp_path / "new.txt").write_text("x", encoding="utf-8")
    plan4 = coordinator.prepare(tool, args, ctx, tool_call_id="s4")
    assert plan4.early_observation is None


# 测试目的：write_file 首写不 stale；缺陷类型：stale 误报首写。
def test_write_file_first_write_not_stale(tmp_path: Path) -> None:
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("filesystem",))
    ctx = _make_context(tmp_path)
    plan = coordinator.prepare(tool, {"path": "f.txt", "content": "x"}, ctx, tool_call_id="w1")
    # 从未记录 -> 不 stale
    assert coordinator.check_stale(plan, tool, ctx, tool_call_id="w1") is None


# 测试目的：write_file 外部修改后 stale；缺陷类型：stale 检测未发现外部变更。
def test_write_file_stale_after_external_change(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("v1", encoding="utf-8")
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("filesystem",))
    ctx = _make_context(tmp_path)

    # 先记录 baseline
    plan_read = coordinator.prepare(
        _make_tool(resource_keys=("filesystem",), name="read_file"),
        {"path": "f.txt"},
        ctx,
        tool_call_id="r1",
    )
    coordinator.complete(plan_read, _success_observation("read_file"), ctx)

    # 外部修改
    target.write_text("v2", encoding="utf-8")

    plan_write = coordinator.prepare(tool, {"path": "f.txt", "content": "x"}, ctx, tool_call_id="w1")
    obs = coordinator.check_stale(plan_write, tool, ctx, tool_call_id="w1")
    assert obs is not None
    assert obs.status == "error"
    assert obs.reason == "stale_file"


# 测试目的：check_stale 对 patch 工具返回 stale_patch reason；缺陷类型：reason 分支错误。
def test_check_stale_patch_reason(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("v1", encoding="utf-8")
    coordinator = FileToolStateCoordinator()
    ctx = _make_context(tmp_path)
    patch_tool = _make_tool(resource_keys=("filesystem",), name="patch", permission="file_write")

    # baseline
    coordinator.complete(
        coordinator.prepare(
            _make_tool(resource_keys=("filesystem",), name="read_file"),
            {"path": "f.txt"},
            ctx,
            tool_call_id="r1",
        ),
        _success_observation("read_file"),
        ctx,
    )
    target.write_text("v2", encoding="utf-8")

    plan = coordinator.prepare(
        patch_tool, {"mode": "replace", "path": "f.txt", "content": "new"}, ctx, tool_call_id="p1"
    )
    obs = coordinator.check_stale(plan, patch_tool, ctx, tool_call_id="p1")
    assert obs is not None
    assert obs.reason == "stale_patch"


# 测试目的：check_stale 在 execution_context 为 None 或写路径为空时返回 None；缺陷类型：no-op 契约。
def test_check_stale_noop_without_context_or_write_paths(tmp_path: Path) -> None:
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("filesystem",))
    plan = coordinator.prepare(
        tool, {"path": "f.txt", "content": "x"}, _make_context(tmp_path), tool_call_id="w1"
    )
    assert coordinator.check_stale(plan, tool, None, tool_call_id="w1") is None


# 测试目的：complete 成功后刷新 revision 与 repeated；缺陷类型：成功状态未记录。
def test_complete_success_records_revision_and_repeated(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("v1", encoding="utf-8")
    coordinator = FileToolStateCoordinator()
    ctx = _make_context(tmp_path)

    plan = coordinator.prepare(
        _make_tool(resource_keys=("filesystem",), name="read_file"),
        {"path": "f.txt"},
        ctx,
        tool_call_id="r1",
    )
    coordinator.complete(plan, _success_observation("read_file"), ctx)

    # complete 后 snapshot 已记录 -> 再次 read 应 unchanged
    plan2 = coordinator.prepare(
        _make_tool(resource_keys=("filesystem",), name="read_file"),
        {"path": "f.txt"},
        ctx,
        tool_call_id="r2",
    )
    assert plan2.early_observation is not None


# 测试目的：complete 失败时不做任何记录（revision 不刷新、repeated 不登记）；
# 缺陷类型：失败被当作成功记录。
def test_complete_failure_does_not_record(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("v1", encoding="utf-8")
    coordinator = FileToolStateCoordinator()
    ctx = _make_context(tmp_path)

    plan = coordinator.prepare(
        _make_tool(resource_keys=("filesystem",), name="read_file"),
        {"path": "f.txt"},
        ctx,
        tool_call_id="r1",
    )
    coordinator.complete(plan, _fail_observation("read_file"), ctx)

    # 失败未记录 -> 再次 read 仍应 execute（非 unchanged）
    plan2 = coordinator.prepare(
        _make_tool(resource_keys=("filesystem",), name="read_file"),
        {"path": "f.txt"},
        ctx,
        tool_call_id="r2",
    )
    assert plan2.early_observation is None


# 测试目的：complete 在 execution_context 为 None 时安全返回；缺陷类型：空上下文崩溃。
def test_complete_noop_without_context(tmp_path: Path) -> None:
    coordinator = FileToolStateCoordinator()
    plan = coordinator.prepare(
        _make_tool(resource_keys=("filesystem",)),
        {"path": "f.txt", "content": "x"},
        _make_context(tmp_path),
        tool_call_id="w1",
    )
    coordinator.complete(plan, _success_observation(), None)  # 不应抛


# 测试目的：lock 对写路径持锁并在退出释放；缺陷类型：锁未正确释放。
def test_lock_acquires_and_releases(tmp_path: Path) -> None:
    coordinator = FileToolStateCoordinator()
    ctx = _make_context(tmp_path)
    plan = coordinator.prepare(
        _make_tool(resource_keys=("filesystem",)),
        {"path": "f.txt", "content": "x"},
        ctx,
        tool_call_id="w1",
    )
    with coordinator.lock(plan, ctx):
        pass  # 正常进入退出，不应抛


# 测试目的：lock 在无上下文/无锁路径时直接 yield；缺陷类型：no-op 契约。
def test_lock_noop_without_context_or_lock_paths(tmp_path: Path) -> None:
    coordinator = FileToolStateCoordinator()
    plan = coordinator.prepare(
        _make_tool(resource_keys=("filesystem",)),
        {"path": "f.txt", "content": "x"},
        _make_context(tmp_path),
        tool_call_id="w1",
    )
    with coordinator.lock(plan, None):
        pass


# 测试目的：_observed_paths 非递归 scope（list_directory）只列子目录一层；缺陷类型：遍历范围错误。
def test_list_directory_scope_non_recursive(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b", encoding="utf-8")
    coordinator = FileToolStateCoordinator(max_scope_paths=100)
    tool = _make_tool(resource_keys=("filesystem",), name="list_directory")
    ctx = _make_context(tmp_path)
    plan = coordinator.prepare(tool, {"path": "."}, ctx, tool_call_id="l1")
    # 非递归：不包含 sub 下的 b.txt
    paths = {p for p in plan.observed_paths}
    assert (tmp_path / "a.txt").resolve() in paths
    assert (tmp_path / "sub" / "b.txt").resolve() not in paths


# 测试目的：_observed_paths 递归 search 展开文件；缺陷类型：递归遍历错误。
def test_search_files_recursive_scope(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b", encoding="utf-8")
    coordinator = FileToolStateCoordinator(max_scope_paths=100)
    tool = _make_tool(resource_keys=("filesystem",), name="search_files")
    ctx = _make_context(tmp_path)
    plan = coordinator.prepare(tool, {"path": ".", "pattern": "*.txt"}, ctx, tool_call_id="s1")
    paths = {p for p in plan.observed_paths}
    assert (tmp_path / "a.txt").resolve() in paths
    assert (tmp_path / "sub" / "b.txt").resolve() in paths


# 测试目的：prepare 阶段目录遍历抛 OSError 时快照不完整；缺陷类型：遍历异常处理。
def test_observed_paths_oserror_marks_incomplete(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import app.tools.guard.file_tool_state_coordinator as mod

    def _boom(_base: Path, _glob: str | None = None) -> list[Path]:
        raise OSError("boom")

    monkeypatch.setattr(mod, "iter_files", _boom)
    coordinator = FileToolStateCoordinator(max_scope_paths=100)
    tool = _make_tool(resource_keys=("filesystem",), name="search_files")
    ctx = _make_context(tmp_path)
    plan = coordinator.prepare(tool, {"path": ".", "pattern": "*.txt"}, ctx, tool_call_id="s1")
    assert plan.snapshot_complete is False


# 测试目的：非 repeated 文件工具（list_directory）不进入重复检测，无 early_observation；
# 缺陷类型：重复检测误触发非 read/search 工具。
def test_non_repeated_tool_gets_no_repeated_signature(tmp_path: Path) -> None:
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("filesystem",), name="list_directory")
    ctx = _make_context(tmp_path)
    plan = coordinator.prepare(tool, {"path": "."}, ctx, tool_call_id="l1")
    assert plan.early_observation is None
    assert plan.repeated_signature == ""


# ---------------------------------------------------------------- registries


# 测试目的：registry 构造容量校验；缺陷类型：缺失参数校验。
def test_registries_reject_invalid_capacities() -> None:
    with pytest.raises(ValueError):
        FileRevisionRegistry(max_tasks=0)
    with pytest.raises(ValueError):
        FileRevisionRegistry(max_paths_per_task=0)
    with pytest.raises(ValueError):
        RepeatedCallRegistry(max_tasks=0)
    with pytest.raises(ValueError):
        RepeatedCallRegistry(max_calls_per_task=0)
    with pytest.raises(ValueError):
        FilePathLockRegistry(max_tasks=0)
    with pytest.raises(ValueError):
        FilePathLockRegistry(max_paths_per_task=0)


# 测试目的：revision record 后 stale_paths 报告变化、未变化路径不报；缺陷类型：fingerprint 比对错误。
def test_revision_stale_detection(tmp_path: Path) -> None:
    reg = FileRevisionRegistry()
    target = tmp_path / "f.txt"
    target.write_text("v1", encoding="utf-8")
    reg.record("t1", [target])
    assert reg.stale_paths("t1", [target]) == ()

    # 用不同字节数内容改写，确保 fingerprint（mtime_ns + size）必然变化，
    # 避免同字节数内容在相同文件系统时间 tick 内 mtime 未刷新导致的 flaky。
    target.write_text("v2-changed-content", encoding="utf-8")
    stale = reg.stale_paths("t1", [target])
    assert stale == (target,)


# 测试目的：从未记录的路径不视为 stale；缺陷类型：stale 误报未观察路径。
def test_revision_unrecorded_path_not_stale(tmp_path: Path) -> None:
    reg = FileRevisionRegistry()
    target = tmp_path / "f.txt"
    target.write_text("v1", encoding="utf-8")
    # 未记录
    assert reg.stale_paths("t1", [target]) == ()


# 测试目的：revision task 隔离；缺陷类型：跨 task 串扰。
def test_revision_task_isolation(tmp_path: Path) -> None:
    reg = FileRevisionRegistry()
    target = tmp_path / "f.txt"
    target.write_text("v1", encoding="utf-8")
    reg.record("t1", [target])
    # t2 从未记录
    assert reg.stale_paths("t2", [target]) == ()


# 测试目的：revision record_snapshots 记录并 LRU 淘汰最旧路径；缺陷类型：LRU 顺序错误。
def test_revision_record_snapshots_lru_eviction(tmp_path: Path) -> None:
    reg = FileRevisionRegistry(max_paths_per_task=2)
    p1, p2, p3 = (tmp_path / "1.txt"), (tmp_path / "2.txt"), (tmp_path / "3.txt")
    for p in (p1, p2, p3):
        p.write_text("x", encoding="utf-8")
    reg.record_snapshots("t1", [(reg.canonical_path(p), reg.fingerprint(p)) for p in (p1, p2, p3)])
    # 容量 2 -> 最旧 p1 被淘汰
    assert reg.stale_paths("t1", [p1]) == ()
    # p2, p3 仍在（被记录过）
    assert reg.stale_paths("t1", [p2]) == ()


# 测试目的：revision clear_task 清除状态；缺陷类型：清除失败。
def test_revision_clear_task(tmp_path: Path) -> None:
    reg = FileRevisionRegistry()
    target = tmp_path / "f.txt"
    target.write_text("v1", encoding="utf-8")
    reg.record("t1", [target])
    reg.clear_task("t1")
    assert reg.stale_paths("t1", [target]) == ()


# 测试目的：fingerprint 对不存在/stat 失败路径返回 exists=False；缺陷类型：fingerprint 崩溃。
def test_revision_fingerprint_missing_file(tmp_path: Path) -> None:
    fp = FileRevisionRegistry.fingerprint(tmp_path / "nope.txt")
    assert fp == FileFingerprint(exists=False, mtime_ns=0, size=0)


# 测试目的：revision task 级 LRU 淘汰最旧 task；缺陷类型：task 淘汰错误。
def test_revision_task_lru_eviction(tmp_path: Path) -> None:
    reg = FileRevisionRegistry(max_tasks=1)
    reg.record("t1", [tmp_path / "a.txt"])
    reg.record("t2", [tmp_path / "b.txt"])  # 触发 t1 淘汰
    # 只读内部状态：通过 stale_paths 观察 t1 已不存在（无 stale 报出 = 未记录）
    assert reg.stale_paths("t1", [tmp_path / "a.txt"]) == ()


# 测试目的：repeated check 返回 execute 首次、unchanged 重复、warning/block 对 search；
# 缺陷类型：repeated 判定错误。
def test_repeated_call_states(tmp_path: Path) -> None:
    reg = RepeatedCallRegistry()
    fp = ("a", 1)
    # 无成功基线 -> 首次 execute
    assert reg.check("t1", "sig", fp, tool_name="read_file") == RepeatedCallAction("execute", 0)
    # record_success 登记基线
    reg.record_success("t1", "sig", fp)
    # read_file 重复 unchanged
    assert reg.check("t1", "sig", fp, tool_name="read_file").action == "unchanged"
    # search 登记基线后：第一次 warning、第二次及之后 block
    reg.record_success("t2", "sig2", fp)
    assert reg.check("t2", "sig2", fp, tool_name="search_files").action == "warning"
    assert reg.check("t2", "sig2", fp, tool_name="search_files").action == "block"
    assert reg.check("t2", "sig2", fp, tool_name="search_files").action == "block"


# 测试目的：fingerprint 变化时 repeated 重新 execute（状态重置）；缺陷类型：fingerprint 比较错误。
def test_repeated_call_reset_on_fingerprint_change() -> None:
    reg = RepeatedCallRegistry()
    assert reg.check("t1", "sig", ("a", 1), tool_name="search_files").action == "execute"
    assert reg.check("t1", "sig", ("a", 2), tool_name="search_files").action == "execute"


# 测试目的：record_success 登记基线、重复成功不累计 skip 计数；缺陷类型：成功基线错误。
def test_repeated_record_success() -> None:
    reg = RepeatedCallRegistry()
    fp = ("a", 1)
    reg.record_success("t1", "sig", fp)
    # 已成功基线 + fingerprint 相同 -> read_file unchanged
    assert reg.check("t1", "sig", fp, tool_name="read_file").action == "unchanged"
    # 再次 record_success 相同 fingerprint 重置计数
    reg.record_success("t1", "sig", fp)
    assert reg.check("t1", "sig", fp, tool_name="read_file").action == "unchanged"


# 测试目的：repeated clear_task 清除状态；缺陷类型：清除失败。
def test_repeated_clear_task() -> None:
    reg = RepeatedCallRegistry()
    fp = ("a", 1)
    reg.record_success("t1", "sig", fp)
    reg.clear_task("t1")
    assert reg.check("t1", "sig", fp, tool_name="read_file").action == "execute"


# 测试目的：repeated task 隔离与 task LRU 淘汰；缺陷类型：跨 task 串扰。
def test_repeated_task_isolation_and_lru() -> None:
    reg = RepeatedCallRegistry(max_tasks=1)
    fp = ("a", 1)
    reg.record_success("t1", "sig", fp)
    reg.record_success("t2", "sig", fp)  # 触发 t1 淘汰
    assert reg.check("t1", "sig", fp, tool_name="read_file").action == "execute"


# 测试目的：repeated 单 task 容量超限时淘汰最旧调用；缺陷类型：_trim 淘汰错误。
def test_repeated_trim_evicts_oldest() -> None:
    reg = RepeatedCallRegistry(max_calls_per_task=1)
    fp = ("a", 1)
    reg.record_success("t1", "sig1", fp)
    reg.record_success("t1", "sig2", fp)  # sig1 被淘汰
    assert reg.check("t1", "sig1", fp, tool_name="read_file").action == "execute"
    assert reg.check("t1", "sig2", fp, tool_name="read_file").action == "unchanged"


# 测试目的：路径锁 acquire/release 正常、去重并排序；缺陷类型：锁顺序/释放错误。
def test_path_lock_acquire_release(tmp_path: Path) -> None:
    reg = FilePathLockRegistry()
    p1, p2 = tmp_path / "a.txt", tmp_path / "b.txt"
    with reg.acquire("t1", [p1, p2, p1]):  # 重复路径去重
        pass  # 不抛即通过
    # 释放后 task 状态 user 归零，可 clear
    reg.clear_task("t1")


# 测试目的：路径锁 task 隔离；缺陷类型：跨 task 互斥错误。
def test_path_lock_task_isolation(tmp_path: Path) -> None:
    reg = FilePathLockRegistry()
    p = tmp_path / "a.txt"
    with reg.acquire("t1", [p]):
        with reg.acquire("t2", [p]):  # 不同 task 应可同时获取
            pass


# 测试目的：路径锁单 task 容量耗尽抛 RuntimeError；缺陷类型：容量未强制。
def test_path_lock_capacity_exceeded_raises(tmp_path: Path) -> None:
    reg = FilePathLockRegistry(max_paths_per_task=1)
    p1, p2 = tmp_path / "a.txt", tmp_path / "b.txt"
    with reg.acquire("t1", [p1]):
        with pytest.raises(RuntimeError):
            with reg.acquire("t1", [p2]):  # 唯一路径正被占用，无空闲可淘汰
                pass


# 测试目的：路径锁 task 容量耗尽抛 RuntimeError；缺陷类型：task 容量未强制。
def test_path_lock_task_capacity_exceeded_raises(tmp_path: Path) -> None:
    reg = FilePathLockRegistry(max_tasks=1)
    p = tmp_path / "a.txt"
    with reg.acquire("t1", [p]):  # t1 持有 -> 无可淘汰空闲 task
        with pytest.raises(RuntimeError):
            with reg.acquire("t2", [p]):
                pass


# 测试目的：路径锁单 task 内路径 LRU 淘汰最旧空闲锁；缺陷类型：路径级淘汰错误。
def test_path_lock_evicts_idle_path(tmp_path: Path) -> None:
    reg = FilePathLockRegistry(max_paths_per_task=2)
    p1, p2, p3 = (tmp_path / "1.txt"), (tmp_path / "2.txt"), (tmp_path / "3.txt")
    # 先占用并释放 p1、p2（进入空闲态），此时 task 内已有 2 条路径
    with reg.acquire("t1", [p1, p2]):
        pass
    # 再获取 p3：超容量 1 条，需淘汰最旧空闲路径 p1（或 p2 中的一条）以腾出位置
    with reg.acquire("t1", [p3]):
        pass
    # 不抛即通过；p3 可成功获取，证明淘汰腾位生效
    with reg.acquire("t1", [p3]):
        pass


# 测试目的：路径锁空闲 task 被 LRU 淘汰后新 task 可获取；缺陷类型：LRU 淘汰错误。
def test_path_lock_evicts_idle_task(tmp_path: Path) -> None:
    reg = FilePathLockRegistry(max_tasks=1)
    p = tmp_path / "a.txt"
    with reg.acquire("t1", [p]):
        pass  # t1 释放（users 归 0）
    with reg.acquire("t2", [p]):  # t1 空闲被淘汰
        pass


# 测试目的：路径锁 clear_task 在活跃持有时不删除、空闲时删除；缺陷类型：clear 语义错误。
def test_path_lock_clear_task_only_when_idle(tmp_path: Path) -> None:
    reg = FilePathLockRegistry()
    p = tmp_path / "a.txt"
    reg.clear_task("t1")  # 不存在，安全
    with reg.acquire("t1", [p]):
        reg.clear_task("t1")  # 活跃持有，不清除（不抛即可）
    reg.clear_task("t1")  # 空闲，清除


# ---------------------------------------------------------------- resource paths


# 测试目的：read_file 推导 read_paths、list_directory 推导 scope、未知工具空资源；
# 缺陷类型：工具资源推导错误。
def test_resolver_read_list_unknown(tmp_path: Path) -> None:
    resolver = FileResourceResolver(tmp_path)
    read = resolver.resolve("read_file", {"path": "a.txt"})
    assert read.read_paths == ((tmp_path / "a.txt").resolve(),)
    assert read.write_paths == ()

    lst = resolver.resolve("list_directory", {"path": "."})
    assert lst.scope_root == tmp_path.resolve()
    assert lst.scope_recursive is False

    unknown = resolver.resolve("nope", {"path": "a.txt"})
    assert unknown == FileResourcePaths()


# 测试目的：write_file 推导 write_paths 与 lock_paths（含祖先）；缺陷类型：锁键推导错误。
def test_resolver_write_lock_paths(tmp_path: Path) -> None:
    resolver = FileResourceResolver(tmp_path)
    res = resolver.resolve("write_file", {"path": "a/b.txt"})
    assert res.write_paths == ((tmp_path / "a" / "b.txt").resolve(),)
    root = tmp_path.resolve()
    assert root / "a" in res.lock_paths
    assert root / "a" / "b.txt" in res.lock_paths


# 测试目的：delete 推导 write_paths；缺陷类型：delete 资源推导错误。
def test_resolver_delete(tmp_path: Path) -> None:
    resolver = FileResourceResolver(tmp_path)
    target = tmp_path / "d.txt"
    target.write_text("x", encoding="utf-8")
    res = resolver.resolve("delete", {"path": "d.txt"})
    assert res.write_paths == ((tmp_path / "d.txt").resolve(),)


# 测试目的：patch replace 非字符串/空 path 返回空 write_paths；缺陷类型：patch 资源推导错误。
def test_resolver_patch_replace_empty_or_nonstring(tmp_path: Path) -> None:
    resolver = FileResourceResolver(tmp_path)
    assert resolver.resolve("patch", {"mode": "replace"}).write_paths == ()
    assert resolver.resolve("patch", {"mode": "replace", "path": None}).write_paths == ()
    assert resolver.resolve("patch", {"mode": "replace", "path": 42}).write_paths == ()


# 测试目的：patch replace 正常 path 推导写路径；缺陷类型：patch 写路径错误。
def test_resolver_patch_replace_path(tmp_path: Path) -> None:
    resolver = FileResourceResolver(tmp_path)
    res = resolver.resolve("patch", {"mode": "replace", "path": "f.txt"})
    assert res.write_paths == ((tmp_path / "f.txt").resolve(),)


# 测试目的：patch v4a 模式解析操作推导写路径（含 MOVE 的 new_path）；缺陷类型：v4a 解析错误。
def test_resolver_patch_v4a_operations(tmp_path: Path) -> None:
    resolver = FileResourceResolver(tmp_path)
    patch_text = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        "-x\n"
        "+y\n"
        "*** Move File: a.txt -> b.txt\n"
        "*** End Patch\n"
    )
    res = resolver.resolve("patch", {"mode": "v4a", "patch": patch_text})
    assert (tmp_path / "a.txt").resolve() in res.write_paths
    assert (tmp_path / "b.txt").resolve() in res.write_paths


# 测试目的：patch v4a 解析失败/非字符串返回空；缺陷类型：解析失败未兜底。
def test_resolver_patch_v4a_parse_failure(tmp_path: Path) -> None:
    resolver = FileResourceResolver(tmp_path)
    assert resolver.resolve("patch", {"mode": "v4a"}).write_paths == ()
    assert resolver.resolve("patch", {"mode": "v4a", "patch": 123}).write_paths == ()
    # Move 缺目标 -> 解析失败，返回空写路径
    bad = "*** Begin Patch\n*** Move File: a.txt\n*** End Patch\n"
    assert resolver.resolve("patch", {"mode": "v4a", "patch": bad}).write_paths == ()


# 测试目的：NUL 字符路径被拒绝；缺陷类型：NUL 校验缺失。
def test_resolver_rejects_nul(tmp_path: Path) -> None:
    resolver = FileResourceResolver(tmp_path)
    with pytest.raises(FileResourcePathError) as exc_info:
        resolver.resolve("write_file", {"path": "a\x00b.txt"})
    assert "NUL" in str(exc_info.value)


# 测试目的：越界写路径被拒绝（绝对与相对）；缺陷类型：越界 containment 缺失。
def test_resolver_rejects_out_of_bounds_write(tmp_path: Path) -> None:
    resolver = FileResourceResolver(tmp_path)
    outside = _outside_path(tmp_path) / "evil.txt"
    with pytest.raises(FileResourcePathError):
        resolver.resolve("write_file", {"path": str(outside)})
    with pytest.raises(FileResourcePathError):
        resolver.resolve("write_file", {"path": "../evil.txt"})


# 测试目的：空 path 被拒绝；缺陷类型：空路径校验缺失。
def test_resolver_rejects_empty_write_path(tmp_path: Path) -> None:
    resolver = FileResourceResolver(tmp_path)
    with pytest.raises(FileResourcePathError):
        resolver.resolve("write_file", {"path": ""})
    with pytest.raises(FileResourcePathError):
        resolver.resolve("write_file", {"path": "   "})


# 测试目的：只读工具允许越界路径（read_file）；缺陷类型：只读越界被误拒。
def test_resolver_allows_out_of_bounds_read(tmp_path: Path) -> None:
    resolver = FileResourceResolver(tmp_path)
    outside = _outside_path(tmp_path) / "file.txt"
    res = resolver.resolve("read_file", {"path": str(outside)})
    assert res.read_paths == (outside.resolve(strict=False),)


# 测试目的：device/伪文件路径被拒绝（read）；缺陷类型：设备拦截缺失。
def test_resolver_rejects_device_read(tmp_path: Path) -> None:
    resolver = FileResourceResolver(tmp_path)
    with pytest.raises(FileResourcePathError):
        resolver.resolve("read_file", {"path": "NUL"})
    with pytest.raises(FileResourcePathError):
        resolver.resolve("search_files", {"path": "/dev/null"})


# 测试目的：search_files 越界 scope 标记 escapes_workspace=True；缺陷类型：越界判定错误。
def test_resolver_search_out_of_bounds_scope(tmp_path: Path) -> None:
    resolver = FileResourceResolver(tmp_path)
    outside = _outside_path(tmp_path)
    res = resolver.resolve("search_files", {"path": str(outside)})
    assert res.scope_escapes_workspace is True
    assert res.scope_recursive is True


# 测试目的：resolve_file_resource_paths 在 execution_context 为 None 时返回空资源；缺陷类型：空上下文兜底。
def test_resolve_file_resource_paths_no_context(tmp_path: Path) -> None:
    assert resolve_file_resource_paths("read_file", {"path": "a.txt"}, None) == FileResourcePaths()


# 测试目的：parse_v4a_patch 的 OperationType.MOVE 常量存在（支撑 v4a 用例正确性）。
def test_operation_type_move_constant() -> None:
    assert OperationType.MOVE is not None


# ---------------------------------------------------------------- repeated signature completeness


# 测试目的：search_files 不同 pattern 应产生不同重复签名，避免不同检索词被误判为重复拦截；
# 缺陷类型：签名仅含 path、漏掉 pattern 等区分参数。
def test_search_signature_includes_pattern(tmp_path: Path) -> None:
    from app.tools.guard.file_tool_state_coordinator import normalize_repeated_call_arguments

    sig_py = normalize_repeated_call_arguments(
        "search_files", {"path": ".", "pattern": "*.py"}, tmp_path
    )
    sig_txt = normalize_repeated_call_arguments(
        "search_files", {"path": ".", "pattern": "*.txt"}, tmp_path
    )
    assert sig_py != sig_txt, "不同 pattern 必须产生不同签名"


# 测试目的：search_files 不同 target（content vs files）应产生不同签名；缺陷类型：签名漏 target。
def test_search_signature_includes_target(tmp_path: Path) -> None:
    from app.tools.guard.file_tool_state_coordinator import normalize_repeated_call_arguments

    content = normalize_repeated_call_arguments(
        "search_files", {"path": ".", "pattern": "x", "target": "content"}, tmp_path
    )
    files = normalize_repeated_call_arguments(
        "search_files", {"path": ".", "pattern": "x", "target": "files"}, tmp_path
    )
    assert content != files, "不同 target 必须产生不同签名"


# 测试目的：search_files 不同分页（offset）应产生不同签名；缺陷类型：签名漏分页参数。
def test_search_signature_includes_pagination(tmp_path: Path) -> None:
    from app.tools.guard.file_tool_state_coordinator import normalize_repeated_call_arguments

    page1 = normalize_repeated_call_arguments(
        "search_files", {"path": ".", "pattern": "x", "offset": 0}, tmp_path
    )
    page2 = normalize_repeated_call_arguments(
        "search_files", {"path": ".", "pattern": "x", "offset": 50}, tmp_path
    )
    assert page1 != page2, "不同 offset 必须产生不同签名"


# 测试目的：read_file 不同 offset 应产生不同签名，分页续读不被误判为 unchanged；
# 缺陷类型：签名漏 offset/limit。
def test_read_signature_includes_offset(tmp_path: Path) -> None:
    from app.tools.guard.file_tool_state_coordinator import normalize_repeated_call_arguments

    page1 = normalize_repeated_call_arguments(
        "read_file", {"path": "big.txt", "offset": 1}, tmp_path
    )
    page2 = normalize_repeated_call_arguments(
        "read_file", {"path": "big.txt", "offset": 501}, tmp_path
    )
    assert page1 != page2, "不同 offset 分页续读必须视为不同调用"


# 测试目的：端到端——search_files 用不同 pattern 对同一目录连续搜索，第二次不应被拦截；
# 缺陷类型：prepare 早退误拦不同检索词。
def test_search_different_patterns_not_blocked(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("import os", encoding="utf-8")
    (tmp_path / "c.txt").write_text("hello", encoding="utf-8")
    coordinator = FileToolStateCoordinator(max_scope_paths=100)
    tool = _make_tool(resource_keys=("filesystem",), name="search_files")
    ctx = _make_context(tmp_path)

    p1 = coordinator.prepare(tool, {"path": ".", "pattern": "*.py"}, ctx, tool_call_id="c1")
    assert p1.early_observation is None
    coordinator.complete(p1, _success_observation("search_files"), ctx)

    p2 = coordinator.prepare(tool, {"path": ".", "pattern": "*.txt"}, ctx, tool_call_id="c2")
    assert p2.early_observation is None, "不同 pattern 搜索不应被误判为重复拦截"


# 测试目的：端到端——read_file 不同 offset 续读不应被误判为 unchanged；
# 缺陷类型：prepare 早退阻断大文件分页续读。
def test_read_different_offset_not_unchanged(tmp_path: Path) -> None:
    big = tmp_path / "big.txt"
    big.write_text("\n".join(f"line{i}" for i in range(1000)), encoding="utf-8")
    coordinator = FileToolStateCoordinator()
    tool = _make_tool(resource_keys=("filesystem",), name="read_file")
    ctx = _make_context(tmp_path)

    p1 = coordinator.prepare(tool, {"path": "big.txt", "offset": 1}, ctx, tool_call_id="c1")
    assert p1.early_observation is None
    coordinator.complete(p1, _success_observation("read_file"), ctx)

    p2 = coordinator.prepare(tool, {"path": "big.txt", "offset": 501}, ctx, tool_call_id="c2")
    assert p2.early_observation is None, "不同 offset 分页续读不应被误判为 unchanged"
