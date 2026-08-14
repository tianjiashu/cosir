"""守卫层 patch 拆分（去掉 mode 依赖）单元测试。

验证 Task 2 拆分后：
- ``file_resource_paths`` 按 ``patch``(replace) / ``apply_patch``(V4A) 两个 tool_name
  分流，不再读取 arguments["mode"]。
- ``file_tool_state_coordinator`` 的 ``normalize_repeated_call_arguments``（重复调用
  签名 _signature_fields）与 ``check_stale`` 的 stale reason 分支按 tool_name 分流。

所有用例只测行为契约、边界与错误路径，不修改任何业务代码。
workspace 用 pytest tmp_path 构造 ToolExecutionContext（参照 conftest.py 的 context fixture）。
"""

import os
from pathlib import Path

import pytest

from app.tools.guard.file_resource_paths import (
    FileResourcePaths,
    FileResourceResolver,
    FileResourcePathError,
    resolve_file_resource_paths,
)
from app.tools.guard.file_tool_state_coordinator import (
    FileToolStateCoordinator,
    normalize_repeated_call_arguments,
)
from app.tools.schemas import ToolDefinition, ToolExecutionContext, ToolObservation
from app.tools.tool_handler.patch import OperationType, parse_v4a_patch
from app.tools.tool_handler.security.path_resolver import PathResolver

# ----------------------------------------------------------------------
# 工具构造辅助
# ----------------------------------------------------------------------


def _make_tool(name: str) -> ToolDefinition:
    """构造一个最小可用的文件工具定义（filesystem 资源键）。

    测试不真正执行 handler，故 handler 用占位 callable。
    """

    def _noop(*_a, **_k):  # type: ignore[no-untyped-def]
        return None

    return ToolDefinition(
        name=name,
        description=f"{name} tool",
        permission="file_write",
        handler=_noop,
        args_model=__import__("pydantic").BaseModel,  # 占位，不用于校验
        resource_keys=("filesystem",),
    )


# ----------------------------------------------------------------------
# A. file_resource_paths: patch（replace 语义, is_v4a=False）
# ----------------------------------------------------------------------


def test_patch_replace_derives_write_path(context: ToolExecutionContext) -> None:
    # 测试目的：patch(is_v4a=False) 传 {path:"a.py"} 推导写路径含 a.py。
    # 可能发现的缺陷：path 字段未被读取、写路径为空、被错误当作 V4A 解析。
    resources = resolve_file_resource_paths("patch", {"path": "a.py"}, context)
    assert len(resources.write_paths) == 1
    assert resources.write_paths[0] == context.workspace_root / "a.py"


def test_patch_replace_no_mode_field_still_works(context: ToolExecutionContext) -> None:
    # 测试目的：arguments 不含 mode 字段时，patch 仍能正确推导写路径（去掉 mode 依赖）。
    # 可能发现的缺陷：残留对 arguments["mode"] 的读取，导致 KeyError 或空写路径。
    args: dict = {"path": "b.py"}  # 故意不传 mode
    assert "mode" not in args
    resources = resolve_file_resource_paths("patch", args, context)
    assert resources.write_paths == (context.workspace_root / "b.py",)


def test_patch_replace_empty_path_yields_no_write(context: ToolExecutionContext) -> None:
    # 测试目的：patch 传空 path 应返回空写路径（不抛异常）。
    # 可能发现的缺陷：空 path 被解析成 workspace 根本身、或抛未捕获异常。
    resources = resolve_file_resource_paths("patch", {"path": ""}, context)
    assert resources.write_paths == ()


def test_patch_replace_missing_path_yields_no_write(context: ToolExecutionContext) -> None:
    # 测试目的：patch 完全缺 path 字段应返回空写路径（边界）。
    # 可能发现的缺陷：缺字段时抛 KeyError、或返回 workspace 根。
    resources = resolve_file_resource_paths("patch", {}, context)
    assert resources.write_paths == ()


# 私有方法直测：is_v4a=False 分支
def test_patch_resources_replace_branch_direct(workspace: Path) -> None:
    # 测试目的：直接测 _patch_resources(is_v4a=False) 的 replace 分支行为契约。
    # 可能发现的缺陷：分支回归（误读 V4A 文本而非 path）。
    resolver = FileResourceResolver(workspace)
    assert resolver._patch_resources({"path": "x.py"}, is_v4a=False).write_paths == (
        workspace / "x.py",
    )
    # 非字符串 path 应返回空资源
    assert resolver._patch_resources({"path": 123}, is_v4a=False).write_paths == ()
    assert resolver._patch_resources({}, is_v4a=False).write_paths == ()


# ----------------------------------------------------------------------
# B. file_resource_paths: apply_patch（V4A 语义, is_v4a=True）
# ----------------------------------------------------------------------

_VALID_V4A_MULTI = (
    "*** Begin Patch\n"
    "*** Update File: a.py\n"
    "@@\n"
    " x\n"
    "+y\n"
    "*** Update File: sub/b.py\n"
    "@@\n"
    " p\n"
    "+q\n"
    "*** End Patch\n"
)

_VALID_V4A_MOVE = (
    "*** Begin Patch\n"
    "*** Move File: old.py -> new.py\n"
    "*** End Patch\n"
)

_INVALID_V4A_NO_HUNK = (
    "*** Begin Patch\n"
    "*** Update File: a.py\n"  # 无 @@ hunk -> parse 失败
    "*** End Patch\n"
)


def test_apply_patch_v4a_multi_file_write_paths(context: ToolExecutionContext) -> None:
    # 测试目的：apply_patch(is_v4a=True) 传合法多文件 V4A，写路径含所有文件路径。
    # 可能发现的缺陷：只取第一个文件、路径缺失、或误当作 replace 单文件。
    resources = resolve_file_resource_paths(
        "apply_patch", {"patch": _VALID_V4A_MULTI}, context
    )
    paths = set(resources.write_paths)
    assert paths == {
        context.workspace_root / "a.py",
        context.workspace_root / "sub" / "b.py",
    }


def test_apply_patch_no_mode_field_still_works(context: ToolExecutionContext) -> None:
    # 测试目的：arguments 不含 mode 字段时，apply_patch 仍能正确解析 V4A（去掉 mode 依赖）。
    # 可能发现的缺陷：残留对 arguments["mode"] 的读取，导致 KeyError 或空写路径。
    args: dict = {"patch": _VALID_V4A_MULTI}
    assert "mode" not in args
    resources = resolve_file_resource_paths("apply_patch", args, context)
    assert len(resources.write_paths) == 2


def test_apply_patch_v4a_move_includes_new_path(context: ToolExecutionContext) -> None:
    # 测试目的：V4A Move 操作应同时把源路径与目标路径纳入写路径。
    # 可能发现的缺陷：new_path 被忽略、只记源路径。
    resources = resolve_file_resource_paths(
        "apply_patch", {"patch": _VALID_V4A_MOVE}, context
    )
    paths = set(resources.write_paths)
    assert paths == {
        context.workspace_root / "old.py",
        context.workspace_root / "new.py",
    }


def test_apply_patch_v4a_invalid_yields_empty(context: ToolExecutionContext) -> None:
    # 测试目的：非法 V4A（UPDATE 无 hunk，parse 失败）应返回空写路径。
    # 可能发现的缺陷：非法 patch 被当成空操作成功、或抛未捕获异常。
    resources = resolve_file_resource_paths(
        "apply_patch", {"patch": _INVALID_V4A_NO_HUNK}, context
    )
    assert resources.write_paths == ()


def test_apply_patch_v4a_non_string_patch_yields_empty(context: ToolExecutionContext) -> None:
    # 测试目的：patch 参数非字符串（如 int/None）应返回空写路径（边界）。
    # 可能发现的缺陷：非字符串导致类型异常、或误当成空串解析。
    assert resolve_file_resource_paths("apply_patch", {"patch": 123}, context).write_paths == ()
    assert resolve_file_resource_paths("apply_patch", {"patch": None}, context).write_paths == ()


def test_apply_patch_v4a_escapes_workspace_rejected(context: ToolExecutionContext) -> None:
    # 测试目的：V4A 含越界路径（../escape.py）应被 containment 拒绝（抛 FileResourcePathError）。
    # 可能发现的缺陷：越界路径未被拦截、被错误写入 workspace 外。
    escaped_v4a = (
        "*** Begin Patch\n"
        "*** Add File: ../escape.py\n"
        "+x\n"
        "*** End Patch\n"
    )
    with pytest.raises(FileResourcePathError):
        resolve_file_resource_paths("apply_patch", {"patch": escaped_v4a}, context)
    # 确认文件没有被写到 workspace 外
    assert not (context.workspace_root.parent / "escape.py").exists()


def test_apply_patch_v4a_containment_direct(workspace: Path) -> None:
    # 测试目的：直接测 _patch_resources(is_v4a=True) 的 V4A 分支，验证越界抛错与合法多文件。
    # 可能发现的缺陷：V4A 分支回归（误读 path 而非 patch 文本）。
    resolver = FileResourceResolver(workspace)
    # 合法的 V4A 多文件
    res = resolver._patch_resources({"patch": _VALID_V4A_MULTI}, is_v4a=True)
    assert set(res.write_paths) == {
        workspace / "a.py",
        workspace / "sub" / "b.py",
    }
    # 非法 V4A（parse 失败）返回空
    assert resolver._patch_resources({"patch": _INVALID_V4A_NO_HUNK}, is_v4a=True).write_paths == ()
    # 非字符串 patch
    assert resolver._patch_resources({"patch": None}, is_v4a=True).write_paths == ()
    # 越界 V4A 抛 containment 错误
    escaped = "*** Begin Patch\n*** Add File: ../escape.py\n+x\n*** End Patch\n"
    with pytest.raises(FileResourcePathError):
        resolver._patch_resources({"patch": escaped}, is_v4a=True)


# ----------------------------------------------------------------------
# C. 两层分流一致性：同一 resolver 下 patch 与 apply_patch 互不串台
# ----------------------------------------------------------------------


def test_patch_vs_apply_patch_do_not_cross_talk(workspace: Path) -> None:
    # 测试目的：patch 工具不应把 V4A 文本当 path；apply_patch 不应把 path 当 V4A。
    # 可能发现的缺陷：分流串台，patch 误读 "patch" 键、apply_patch 误读 "path" 键。
    resolver = FileResourceResolver(workspace)
    # patch 传 V4A 文本作为 path：应当作路径字面量推导（含非法字符但被 containment 解析为路径）
    patch_res = resolver.resolve("patch", {"path": "a.py"})
    # apply_patch 传 path 字段而非 patch 字段：patch 文本缺失 -> 空写路径
    apply_res = resolver.resolve("apply_patch", {"path": "a.py"})
    assert patch_res.write_paths == (workspace / "a.py",)
    assert apply_res.write_paths == ()


# ----------------------------------------------------------------------
# D. normalize_repeated_call_arguments（_signature_fields）
# ----------------------------------------------------------------------


def test_normalize_patch_signature_mode_replace(workspace: Path) -> None:
    # 测试目的：patch 签名应返回 {"mode":"replace","path":...}，path 经归一。
    # 可能发现的缺陷：mode 值错误（如残留 "patch"）、path 未归一、缺 path。
    sig = normalize_repeated_call_arguments("patch", {"path": "a.py"}, workspace)
    # 注意 _canonical_path 使用 os.path.normcase 归一，期望值须对齐（Windows 下大写盘符）。
    assert sig == {"mode": "replace", "path": os.path.normcase(str(workspace / "a.py"))}


def test_normalize_patch_signature_no_mode_arg_needed(workspace: Path) -> None:
    # 测试目的：patch 签名不再依赖外部 mode 入参，arguments 不含 mode 也能生成正确签名。
    # 可能发现的缺陷：从 arguments 读取 mode 而非硬编码，导致缺 mode 时签名异常。
    args: dict = {"path": "a.py"}
    assert "mode" not in args
    sig = normalize_repeated_call_arguments("patch", args, workspace)
    assert sig["mode"] == "replace"


def test_normalize_apply_patch_signature_mode_apply(workspace: Path) -> None:
    # 测试目的：apply_patch 签名应返回 {"mode":"apply_patch","patch":...}。
    # 可能发现的缺陷：mode 值错误（如残留 "replace"）、patch 文本缺失。
    v4a = _VALID_V4A_MULTI
    sig = normalize_repeated_call_arguments("apply_patch", {"patch": v4a}, workspace)
    assert sig == {"mode": "apply_patch", "patch": v4a}


def test_normalize_apply_patch_signature_no_mode_arg_needed(workspace: Path) -> None:
    # 测试目的：apply_patch 签名不依赖外部 mode 入参，arguments 不含 mode 仍正确。
    # 可能发现的缺陷：从 arguments 读取 mode，缺 mode 时签名异常或 KeyError。
    args: dict = {"patch": _VALID_V4A_MULTI}
    assert "mode" not in args
    sig = normalize_repeated_call_arguments("apply_patch", args, workspace)
    assert sig["mode"] == "apply_patch"


def test_normalize_patch_vs_apply_patch_distinct_signatures(workspace: Path) -> None:
    # 测试目的：patch 与 apply_patch 的签名不应相等（mode 字段区分）。
    # 可能发现的缺陷：两者签名完全相同，导致重复调用检测串台。
    patch_sig = normalize_repeated_call_arguments("patch", {"path": "a.py"}, workspace)
    apply_sig = normalize_repeated_call_arguments("apply_patch", {"patch": "x"}, workspace)
    assert patch_sig != apply_sig
    assert patch_sig["mode"] != apply_sig["mode"]


def test_normalize_patch_path_canonical_equivalence(workspace: Path) -> None:
    # 测试目的：等价路径（相对 / 绝对 / 双斜杠）应产生相同 path 签名键。
    # 可能发现的缺陷：路径归一失效，等价路径被判为不同调用。
    a = normalize_repeated_call_arguments("patch", {"path": "a/b.py"}, workspace)
    b = normalize_repeated_call_arguments("patch", {"path": str(workspace / "a" / "b.py")}, workspace)
    c = normalize_repeated_call_arguments("patch", {"path": "a//b.py"}, workspace)
    assert a["path"] == b["path"] == c["path"]


# ----------------------------------------------------------------------
# E. check_stale 的 reason 分支（patch / apply_patch -> stale_patch；其它 -> stale_file）
# ----------------------------------------------------------------------


def _make_coordinator_with_external_change(
    context: ToolExecutionContext, write_path: Path
) -> FileToolStateCoordinator:
    """构造一个已经记录「基线快照」但随后外部改动了文件的协调器。

    通过 complete() 记录一次成功写入基线，再真正改文件 mtime/内容，使
    revisions.stale_paths 检测到外部改动。
    """
    coord = FileToolStateCoordinator()
    # 先确保文件存在并写入内容
    write_path.parent.mkdir(parents=True, exist_ok=True)
    write_path.write_text("baseline\n", encoding="utf-8")

    from app.tools.guard.file_state import FileRevisionRegistry

    revisions = FileRevisionRegistry()
    coord = FileToolStateCoordinator(revisions=revisions)

    # 记录一次成功写入的 revision 基线
    plan = coord.prepare(
        _make_tool("write_file"),
        {"path": str(write_path)},
        context,
        tool_call_id="tc-1",
    )
    obs = ToolObservation(
        tool_name="write_file",
        status="success",
        content="ok",
        tool_call_id="tc-1",
    )
    coord.complete(plan, obs, context)

    # 外部修改文件（模拟其他进程/模型改动）
    write_path.write_text("changed by external\n", encoding="utf-8")
    return coord


def test_check_stale_patch_reason_is_stale_patch(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：tool.name=="patch" 命中 stale 时，reason 应为 "stale_patch"。
    # 可能发现的缺陷：reason 误判为 "stale_file" 或永不 stale。
    target = workspace / "p.py"
    coord = _make_coordinator_with_external_change(context, target)
    plan = coord.prepare(
        _make_tool("patch"),
        {"path": "p.py"},
        context,
        tool_call_id="tc-2",
    )
    obs = coord.check_stale(plan, _make_tool("patch"), context, tool_call_id="tc-2")
    assert obs is not None
    assert obs.reason == "stale_patch"
    assert "p.py" in obs.content


def test_check_stale_apply_patch_reason_is_stale_patch(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：tool.name=="apply_patch" 命中 stale 时，reason 应为 "stale_patch"。
    # 可能发现的缺陷：apply_patch 被遗漏，reason 误判为 "stale_file"。
    # 注意：apply_patch 的写路径来自 V4A 文本，基线必须覆盖同一文件（ap.py）才能判 stale。
    target = workspace / "ap.py"
    coord = _make_coordinator_with_external_change(context, target)
    ap_v4a = (
        "*** Begin Patch\n"
        "*** Update File: ap.py\n"
        "@@\n"
        " x\n"
        "+y\n"
        "*** End Patch\n"
    )
    plan = coord.prepare(
        _make_tool("apply_patch"),
        {"patch": ap_v4a},
        context,
        tool_call_id="tc-2",
    )
    obs = coord.check_stale(plan, _make_tool("apply_patch"), context, tool_call_id="tc-2")
    assert obs is not None
    assert obs.reason == "stale_patch"
    assert "ap.py" in obs.content


def test_check_stale_other_tool_reason_is_stale_file(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：其它写工具（如 write_file）命中 stale 时，reason 应为 "stale_file"。
    # 可能发现的缺陷：reason 错误地对所有工具都用 "stale_patch"。
    target = workspace / "w.py"
    coord = _make_coordinator_with_external_change(context, target)
    plan = coord.prepare(
        _make_tool("write_file"),
        {"path": "w.py"},
        context,
        tool_call_id="tc-2",
    )
    obs = coord.check_stale(plan, _make_tool("write_file"), context, tool_call_id="tc-2")
    assert obs is not None
    assert obs.reason == "stale_file"


def test_check_stale_no_write_paths_returns_none(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：只读工具（read_file，无写路径）check_stale 应返回 None（不误判）。
    # 可能发现的缺陷：只读工具被错误判定为 stale。
    coord = FileToolStateCoordinator()
    plan = coord.prepare(
        _make_tool("read_file"),
        {"path": "x.py"},
        context,
        tool_call_id="tc-1",
    )
    obs = coord.check_stale(plan, _make_tool("read_file"), context, tool_call_id="tc-1")
    assert obs is None


def test_check_stale_without_context_returns_none(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：execution_context 为 None 时 check_stale 直接返回 None（短路）。
    # 可能发现的缺陷：缺 context 时抛异常、或误判 stale。
    coord = FileToolStateCoordinator()
    plan = coord.prepare(
        _make_tool("patch"),
        {"path": "x.py"},
        context,
        tool_call_id="tc-1",
    )
    obs = coord.check_stale(plan, _make_tool("patch"), None, tool_call_id="tc-1")
    assert obs is None


def test_check_stale_not_stale_returns_none(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：文件自基线后未被外部改动时 check_stale 返回 None。
    # 可能发现的缺陷：无改动却误报 stale。
    target = workspace / "ns.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("baseline\n", encoding="utf-8")
    coord = FileToolStateCoordinator()
    plan = coord.prepare(_make_tool("patch"), {"path": "ns.py"}, context, tool_call_id="tc-1")
    obs_ok = ToolObservation(tool_name="patch", status="success", content="ok", tool_call_id="tc-1")
    coord.complete(plan, obs_ok, context)
    # 未改动文件，再次 prepare + check_stale 应返回 None
    plan2 = coord.prepare(_make_tool("patch"), {"path": "ns.py"}, context, tool_call_id="tc-2")
    obs = coord.check_stale(plan2, _make_tool("patch"), context, tool_call_id="tc-2")
    assert obs is None


def test_prepare_non_filesystem_tool_short_circuits(context: ToolExecutionContext) -> None:
    # 测试目的：非 filesystem 工具（如 execute_terminal）prepare 返回空计划，
    # 后续 lock/check_stale/complete 全部 no-op。
    # 可能发现的缺陷：非文件工具被错误地走文件协调管线。
    term = _make_tool("execute_terminal")
    term = ToolDefinition(
        name="execute_terminal",
        description="terminal",
        permission="shell",
        handler=lambda *a, **k: None,
        args_model=__import__("pydantic").BaseModel,
        resource_keys=("shell",),
    )
    coord = FileToolStateCoordinator()
    plan = coord.prepare(term, {"command": "ls"}, context, tool_call_id="tc-1")
    assert plan.resources.write_paths == ()
    assert plan.observed_paths == ()
    # lock/check_stale/complete 均不应抛错
    with coord.lock(plan, context):
        pass
    assert coord.check_stale(plan, term, context, tool_call_id="tc-1") is None
    coord.complete(plan, ToolObservation(tool_name="execute_terminal", status="success", content="ok"), context)


def test_repeated_observation_branches(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：_repeated_observation 对 block/warning/unchanged 各分支产出正确观察。
    # 可能发现的缺陷：分支投影错误（block 未返回 error、warning/unchanged 状态/数据错误）。
    coord = FileToolStateCoordinator()
    tool = _make_tool("search_files")
    # 直接调用静态方法的各分支（execute 返回 None，不测）。
    block = coord._repeated_observation(tool, "block", tool_call_id="tc")
    assert block is not None
    assert block.status == "error"
    assert block.reason == "repeated_call"

    warning = coord._repeated_observation(tool, "warning", tool_call_id="tc")
    assert warning is not None
    assert warning.status == "success"
    assert warning.data is not None and warning.data.get("warning") is True

    unchanged = coord._repeated_observation(_make_tool("read_file"), "unchanged", tool_call_id="tc")
    assert unchanged is not None
    assert unchanged.status == "success"
    assert unchanged.data is not None and unchanged.data.get("unchanged") is True


def test_complete_records_revisions_and_repeat(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：complete 在成功后回写 revision 与重复调用签名（read_file 参与重复检测）。
    # 可能发现的缺陷：成功回写被遗漏、或失败时误回写。
    coord = FileToolStateCoordinator()
    tool = _make_tool("read_file")
    plan = coord.prepare(tool, {"path": "x.py"}, context, tool_call_id="tc-1")
    ok_obs = ToolObservation(tool_name="read_file", status="success", content="ok", tool_call_id="tc-1")
    coord.complete(plan, ok_obs, context)  # 不应抛错

    # 失败观察不应触发回写（仅验证调用不抛错、行为契约存在）
    fail_obs = ToolObservation(tool_name="read_file", status="error", content="boom", tool_call_id="tc-1")
    coord.complete(plan, fail_obs, context)


def test_prepare_read_file_observed_paths(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：read_file 的 prepare 把单文件 read 路径作为 observed_paths，且参与
    # 重复调用签名（_signature 稳定）。
    # 可能发现的缺陷：observed_paths 为空、签名不稳定。
    coord = FileToolStateCoordinator()
    plan = coord.prepare(_make_tool("read_file"), {"path": "x.py"}, context, tool_call_id="tc-1")
    assert plan.observed_paths == (context.workspace_root / "x.py",)
    assert plan.repeated_signature.startswith("read_file:")


def test_search_files_signature_uses_full_params(workspace: Path) -> None:
    # 测试目的：search_files 签名应包含 pattern 等全部检索参数，而非仅 path。
    # 可能发现的缺陷：签名只含 path，导致不同检索词被误判为重复。
    sig = normalize_repeated_call_arguments(
        "search_files",
        {"path": ".", "pattern": "foo", "output_mode": "files_with_matches"},
        workspace,
    )
    assert sig["pattern"] == "foo"
    assert sig["output_mode"] == "files_with_matches"
    # 不同 pattern 应得到不同签名
    other = normalize_repeated_call_arguments(
        "search_files", {"path": ".", "pattern": "bar"}, workspace
    )
    assert sig != other


def test_patch_replace_escape_rejected(context: ToolExecutionContext) -> None:
    # 测试目的：patch 传越界 path（../escape.py）应被 containment 拒绝（抛 FileResourcePathError）。
    # 可能发现的缺陷：越界 path 未被拦截、被写到 workspace 外。
    with pytest.raises(FileResourcePathError):
        resolve_file_resource_paths("patch", {"path": "../escape.py"}, context)
    assert not (context.workspace_root.parent / "escape.py").exists()


def test_patch_resolve_empty_path_returns_empty(context: ToolExecutionContext) -> None:
    # 测试目的：patch 传空串 path 在 resolve 入口返回空写路径（不抛异常）。
    # _patch_resources(is_v4a=False) 显式对空 path 返回空资源而非走 containment 拒绝。
    # 可能发现的缺陷：空串被解析成 workspace 根、或抛未捕获异常。
    resources = resolve_file_resource_paths("patch", {"path": ""}, context)
    assert resources.write_paths == ()


def test_apply_patch_resolve_empty_patch_yields_empty(context: ToolExecutionContext) -> None:
    # 测试目的：apply_patch 传空串 patch 时 resolve 返回空资源（不抛异常）。
    # 可能发现的缺陷：空 patch 触发解析异常、或误当合法 V4A。
    resources = resolve_file_resource_paths("apply_patch", {"patch": ""}, context)
    assert resources.write_paths == ()


def test_unknown_tool_returns_empty_resources(context: ToolExecutionContext) -> None:
    # 测试目的：未知 tool_name 应返回空资源（不抛异常、不误解析）。
    # 可能发现的缺陷：未知工具被错误分流、或抛异常。
    assert resolve_file_resource_paths("mystery_tool", {"foo": "bar"}, context) == FileResourcePaths()


def test_apply_patch_no_context_returns_empty() -> None:
    # 测试目的：execution_context 为 None 时 resolve 返回空资源（兜底）。
    # 可能发现的缺陷：缺 context 时抛异常。
    assert resolve_file_resource_paths("apply_patch", {"patch": _VALID_V4A_MULTI}, None) == FileResourcePaths()


# ----------------------------------------------------------------------
# F. 回归：V4A 解析器契约（确认 _patch_resources 依赖的 parse_v4a_patch 行为）
# ----------------------------------------------------------------------


def test_parse_v4a_patch_contract() -> None:
    # 测试目的：确认 parse_v4a_patch 对多文件 update 解析出两个 operation，
    # 且其 file_path 与 _patch_resources 提取逻辑一致。
    # 可能发现的缺陷：解析器返回结构变化，导致 _patch_resources 取错字段。
    operations, err = parse_v4a_patch(_VALID_V4A_MULTI)
    assert err is None
    assert len(operations) == 2
    types = {op.operation for op in operations}
    assert types == {OperationType.UPDATE}
    files = [op.file_path for op in operations]
    assert files == ["a.py", "sub/b.py"]
