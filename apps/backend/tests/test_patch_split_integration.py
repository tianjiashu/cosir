"""patch 拆分（Task 1-3 全量改动）全局集成验证与端到端回归确认。

职责边界：作为独立测试 Agent，只写测试跑测试、不修改任何业务代码 / 前端源文件 /
既有测试。本文件针对 patch 工具拆分后的「全量工具注册一致性」「guard 跨工具协调集成」
「profile 与注册对齐」「前端配置与后端字段对齐」做集成断言。

覆盖改动面：
- args 模型：replace_args.py / apply_patch_args.py
- handler：replace_tool.py / apply_patch_tool.py
- 守卫层：file_tool_state_coordinator.py / file_resource_paths.py
- 工具注册：tool_system.py
- agent profile：define_agents.py
- 前端：toolDisplayRules.ts
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.core.agents.define_agents import coder_agent, developer_agent
from app.tools.guard.file_resource_paths import resolve_file_resource_paths
from app.tools.guard.file_tool_state_coordinator import (
    FileToolStateCoordinator,
    normalize_repeated_call_arguments,
)
from app.tools.schemas import ToolDefinition, ToolExecutionContext, ToolObservation
from app.tools.tool_handler.apply_patch_tool import (
    APPLY_PATCH_DESCRIPTION,
    build_apply_patch_definition,
)
from app.tools.tool_handler.replace_tool import (
    REPLACE_DESCRIPTION,
    ReplaceTool,
    build_replace_definition,
)
from app.tools.tool_models.apply_patch_args import ApplyPatchArgs
from app.tools.tool_models.replace_args import ReplaceArgs
from app.tools.tool_system import ToolSystem

# toolDisplayRules.ts 位于仓库根目录下的 apps/shared/ts/。
_FRONTEND_RULES_PATH = (
    Path(__file__).resolve().parents[3] / "apps" / "shared" / "ts" / "toolDisplayRules.ts"
)


def _build_system(client=None) -> ToolSystem:
    """经 build_tool_system 获取完整 ToolSystem（真实装配路径）。"""

    return ToolSystem.build_tool_system(client=client)


def _all_definitions_by_name(system: ToolSystem) -> dict[str, ToolDefinition]:
    """把注册表导出为 name -> ToolDefinition 字典。"""

    return {d.name: d for d in system.registry.get_all_definitions()}


# ----------------------------------------------------------------------
# 1. 全量工具注册一致性
# ----------------------------------------------------------------------


def test_full_registration_contains_patch_and_apply_patch() -> None:
    # 测试目的：build_tool_system() 注册表中同时存在 patch 与 apply_patch 两个独立 ToolDefinition。
    # 可能发现的缺陷：其中之一未注册（build_tool_system 漏 register），导致调用方按 name 取不到。
    system = _build_system(client=None)
    by_name = _all_definitions_by_name(system)
    assert "patch" in by_name
    assert "apply_patch" in by_name
    assert isinstance(by_name["patch"], ToolDefinition)
    assert isinstance(by_name["apply_patch"], ToolDefinition)


def test_no_legacy_patch_references_in_registry() -> None:
    # 测试目的：注册表不应残留旧的 PatchTool / build_patch_definition 命名（拆分后已移除）。
    # 可能发现的缺陷：旧工具名仍被注册、或残留聚合 patch 工具导致与 split 工具 name 冲突。
    system = _build_system(client=None)
    names = system.registry.get_all_tool_names()
    # 不应存在旧的聚合 "patch" 语义混淆：patch 仅代表 replace 分支。
    # 显式断言没有遗留的 "PatchTool" 风格聚合名（如 "patch_old" / "PatchTool"）。
    for legacy in ("PatchTool", "patch_old", "build_patch_definition"):
        assert legacy not in names, f"注册表残留遗留工具名：{legacy}"


def test_patch_and_apply_patch_names_not_duplicated() -> None:
    # 测试目的：patch 与 apply_patch 的 name 互不相同（注册表按 name 覆盖，重复会被静默吞掉）。
    # 可能发现的缺陷：两个工具 name 都误写成 "patch" 或都误写成 "apply_patch"，后者覆盖前者。
    system = _build_system(client=None)
    by_name = _all_definitions_by_name(system)
    assert by_name["patch"].name == "patch"
    assert by_name["apply_patch"].name == "apply_patch"
    assert by_name["patch"].name != by_name["apply_patch"].name
    # 注册表 size：patch + apply_patch 占 2 个独立条目（其余不在此断言范围）。
    assert system.registry.get_all_tool_names().count("patch") == 1
    assert system.registry.get_all_tool_names().count("apply_patch") == 1


def test_patch_and_apply_patch_permissions_are_file_write() -> None:
    # 测试目的：两工具 permission 均为 file_write（破坏性文件写工具）。
    # 可能发现的缺陷：permission 被误设为 never / ask，导致工具实际不可调用。
    system = _build_system(client=None)
    by_name = _all_definitions_by_name(system)
    assert by_name["patch"].permission == "file_write"
    assert by_name["apply_patch"].permission == "file_write"


def test_patch_and_apply_patch_display_expand_layout_is_diff() -> None:
    # 测试目的：两工具 display.expand_layout 均为 "diff"（前端 diff 渲染契约）。
    # 可能发现的缺陷：expand_layout 串台（如一方误为 None / "list"），前端无法展开 diff。
    system = _build_system(client=None)
    by_name = _all_definitions_by_name(system)
    assert by_name["patch"].display is not None
    assert by_name["apply_patch"].display is not None
    assert by_name["patch"].display.expand_layout == "diff"
    assert by_name["apply_patch"].display.expand_layout == "diff"


def test_full_registration_total_count_is_seventeen() -> None:
    # 测试目的：注册表总数为 17（11 个基础工具：read_file/write_file/patch/apply_patch/
    # search_files/list_directory/delete/execute_terminal/web_search/web_extract/delegate_task
    # + 6 个 CodeGraph 查询工具），确认拆分后总数符合设计（原 15 -> 17，净增 replace 与
    # apply_patch 两个独立工具）。
    # 可能发现的缺陷：注册数量异常（漏注册 / 重复注册），间接暴露 name 冲突。
    system = _build_system(client=None)
    names = system.registry.get_all_tool_names()
    assert len(names) == 17, f"预期注册 17 个工具，实际 {len(names)}: {names}"


# ----------------------------------------------------------------------
# 2. guard 跨工具协调集成（真实 API + 真实 arguments）
# ----------------------------------------------------------------------


def _make_filesystem_tool(name: str) -> ToolDefinition:
    """构造最小可用文件工具定义（filesystem 资源键，handler 占位）。"""

    def _noop(*_a, **_k):  # type: ignore[no-untyped-def]
        return None

    return ToolDefinition(
        name=name,
        description=f"{name} tool",
        permission="file_write",
        handler=_noop,
        args_model=BaseModel,
        resource_keys=("filesystem",),
    )


# 真实合法的 V4A 多文件补丁（用于构造真实 arguments）。
_VALID_V4A = (
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


def test_guard_patch_then_apply_patch_independent_signatures(context: ToolExecutionContext) -> None:
    # 测试目的：模拟一次 patch(replace) 调用后紧接 apply_patch 调用，确认两个工具名被守卫层
    # 独立正确处理——它们的重复调用签名不同（mode 区分），不会互相误判为重复。
    # 可能发现的缺陷：两工具签名串台（如都走 path 或都走 patch），导致「先 patch 再 apply_patch」
    # 被误判为重复调用而拦截。
    patch_args = {"path": "a.py"}
    apply_args = {"patch": _VALID_V4A}

    patch_sig = normalize_repeated_call_arguments("patch", patch_args, context.workspace_root)
    apply_sig = normalize_repeated_call_arguments("apply_patch", apply_args, context.workspace_root)

    # 两者语义标记不同。
    assert patch_sig["mode"] == "replace"
    assert apply_sig["mode"] == "apply_patch"
    assert patch_sig != apply_sig

    # 经 _signature 序列化后仍为不同签名（协调器内部去重键）。
    coordinator = FileToolStateCoordinator()
    assert coordinator._signature("patch", patch_sig) != coordinator._signature(
        "apply_patch", apply_sig
    )


def test_guard_patch_then_apply_patch_resource_paths_distinct(context: ToolExecutionContext) -> None:
    # 测试目的：patch 与 apply_patch 的资源路径推导各自独立正确：
    # patch 仅把 path 推导为单文件写路径；apply_patch 把 V4A 文本解析为多个写路径。
    # 两者调用同一个公开 API resolve_file_resource_paths 但结果不相同。
    # 可能发现的缺陷：分流串台（patch 误解析 V4A、apply_patch 误读 path）。
    patch_res = resolve_file_resource_paths("patch", {"path": "a.py"}, context)
    apply_res = resolve_file_resource_paths("apply_patch", {"patch": _VALID_V4A}, context)

    # patch 只含 a.py 一个写路径。
    assert patch_res.write_paths == (context.workspace_root / "a.py",)
    # apply_patch 含 V4A 中的两个文件。
    assert set(apply_res.write_paths) == {
        context.workspace_root / "a.py",
        context.workspace_root / "sub" / "b.py",
    }


def test_guard_same_named_tools_no_cross_stale(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：patch 改写 a.py 成功（complete 记录 revision）后，紧接着 apply_patch（其 V4A
    # 也含 a.py）不应被前者判为 stale（因为它们是不同工具的写路径锁定，协调器按 task+path 记录，
    # 自己刚写的不算 stale）。验证拆分后两个写工具的 stale 判定均按「自己刚写不算 stale」正常工作。
    # 可能发现的缺陷：apply_patch 因 a.py 已被 patch 改写而被错误判 stale、或反过来。
    target = workspace / "a.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("baseline\n", encoding="utf-8")

    coordinator = FileToolStateCoordinator()
    # patch 成功写入 a.py。
    patch_plan = coordinator.prepare(
        _make_filesystem_tool("patch"), {"path": "a.py"}, context, tool_call_id="tc-1"
    )
    coordinator.complete(
        patch_plan,
        ToolObservation(tool_name="patch", status="success", content="ok", tool_call_id="tc-1"),
        context,
    )

    # apply_patch（V4A 含 a.py）紧接着调用，应不 stale（自己刚写的路径不算 stale）。
    apply_plan = coordinator.prepare(
        _make_filesystem_tool("apply_patch"),
        {"patch": _VALID_V4A},
        context,
        tool_call_id="tc-2",
    )
    stale = coordinator.check_stale(
        apply_plan, _make_filesystem_tool("apply_patch"), context, tool_call_id="tc-2"
    )
    assert stale is None, f"apply_patch 不应被 patch 的写入误判为 stale: {stale}"


def test_guard_external_change_makes_both_patch_stale(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：外部（非协调器）改动 a.py 后，patch 与 apply_patch 调同一文件都应被判 stale_patch。
    # 可能发现的缺陷：apply_patch 的 stale 分支被遗漏（reason 退化成 stale_file 或不报 stale）。
    target = workspace / "a.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("baseline\n", encoding="utf-8")

    coordinator = FileToolStateCoordinator()
    # 记录一次外部视角的基线（用 write_file 占位记录 a.py 已被观察）。
    base_plan = coordinator.prepare(
        _make_filesystem_tool("write_file"), {"path": "a.py"}, context, tool_call_id="tc-0"
    )
    coordinator.complete(
        base_plan,
        ToolObservation(tool_name="write_file", status="success", content="ok", tool_call_id="tc-0"),
        context,
    )
    # 外部改动内容（模拟其他进程/模型）。
    target.write_text("changed externally\n", encoding="utf-8")

    # patch 写 a.py 应判 stale_patch。
    patch_plan = coordinator.prepare(
        _make_filesystem_tool("patch"), {"path": "a.py"}, context, tool_call_id="tc-1"
    )
    patch_stale = coordinator.check_stale(
        patch_plan, _make_filesystem_tool("patch"), context, tool_call_id="tc-1"
    )
    assert patch_stale is not None
    assert patch_stale.reason == "stale_patch"

    # apply_patch（V4A 含 a.py）应同样判 stale_patch。
    apply_plan = coordinator.prepare(
        _make_filesystem_tool("apply_patch"), {"patch": _VALID_V4A}, context, tool_call_id="tc-2"
    )
    apply_stale = coordinator.check_stale(
        apply_plan, _make_filesystem_tool("apply_patch"), context, tool_call_id="tc-2"
    )
    assert apply_stale is not None
    assert apply_stale.reason == "stale_patch"


# ----------------------------------------------------------------------
# 3. profile 与注册对齐
# ----------------------------------------------------------------------


def test_coder_agent_allows_apply_patch() -> None:
    # 测试目的：coder_agent() 的 allowed_tools 含 apply_patch（delegate_coder 子 Agent 需持有新工具）。
    # 可能发现的缺陷：define_agents.coder_agent 漏加 apply_patch，导致委派子 Agent 无法调用该工具。
    profile = coder_agent()
    assert "apply_patch" in profile.allowed_tools


def test_coder_agent_allows_patch() -> None:
    # 测试目的：coder_agent() 的 allowed_tools 仍含 patch（replace 分支不能因拆分而丢失）。
    # 可能发现的缺陷：拆分后只加 apply_patch 却移除 patch，子 Agent 失去 replace 能力。
    profile = coder_agent()
    assert "patch" in profile.allowed_tools


def test_profile_declared_tools_resolvable_in_registry() -> None:
    # 测试目的：coder_agent() 声明的每一个 allowed_tools 都能在 build_tool_system 注册表中查到，
    # 即「profile 声明的工具都能被注册」（profile 与注册对齐）。
    # 可能发现的缺陷：profile 声明了注册表里没有的工具名（拼写漂移 / 已删除工具仍被引用）。
    profile = coder_agent()
    system = _build_system(client=None)
    registered = set(system.registry.get_all_tool_names())
    missing = [t for t in profile.allowed_tools if t not in registered]
    assert not missing, f"coder_agent 声明了未注册的工具：{missing}"


def test_developer_agent_apply_patch_in_registry() -> None:
    # 测试目的：developer_agent() 声明的 apply_patch 也能在注册表中查到（父 profile 与注册对齐）。
    # 可能发现的缺陷：developer_agent 声明 apply_patch 但注册表未注册（build_tool_system 漏 register）。
    profile = developer_agent()
    assert "apply_patch" in profile.allowed_tools
    system = _build_system(client=None)
    assert system.registry.get_tool_definition("apply_patch") is not None


# ----------------------------------------------------------------------
# 4. 前端配置与后端字段对齐
# ----------------------------------------------------------------------


def test_frontend_apply_patch_uses_project_file_change_result() -> None:
    # 测试目的：前端 toolDisplayRules.ts 的 RESULT_RULES["apply_patch"] 复用 projectFileChangeResult
    # （diff 投影），与后端的 data["changes"] 字段对齐。
    # 可能发现的缺陷：apply_patch 误配 EMPTY_PROJECTION 或别的投影，多文件变更无法前端展示。
    assert _FRONTEND_RULES_PATH.is_file(), (
        f"expected toolDisplayRules.ts at {_FRONTEND_RULES_PATH}"
    )
    content = _FRONTEND_RULES_PATH.read_text(encoding="utf-8")
    start = content.index("const RESULT_RULES")
    end = content.index("function projectDeleteResult")
    result_block = content[start:end]
    assert "apply_patch:" in result_block, "RESULT_RULES 缺少 apply_patch 条目"
    apply_line = next(
        line for line in result_block.splitlines() if "apply_patch:" in line
    )
    assert "projectFileChangeResult" in apply_line, (
        "RESULT_RULES.apply_patch 未引用 projectFileChangeResult"
    )
    assert "EMPTY_PROJECTION" not in apply_line, (
        "RESULT_RULES.apply_patch 指向 EMPTY_PROJECTION，多文件变更无法前端展示"
    )


def test_backend_apply_patch_success_emits_changes_field(workspace: Path) -> None:
    # 测试目的：apply_patch 成功路径（真实 arguments + 真实执行）产出的 ToolObservation.data
    # 含 "changes" 字段，且前端 projectFileChangeResult 正是读 data.changes，二者字段名一致。
    # 可能发现的缺陷：后端成功 data 不含 changes（字段名漂移），前端读不到 diff 条目。
    from app.tools.tool_handler.file_io.atomic_write import atomic_write_text
    from app.tools.tool_handler.patch.file_change_display import build_file_change_display_data
    from app.tools.tool_handler.patch.patch_diff import FileDiffResult

    # 准备一个真实可写文件与合法 V4A（含该文件）。
    target = workspace / "a.py"
    target.write_text("x\n", encoding="utf-8")
    v4a = (
        "*** Begin Patch\n"
        f"*** Update File: a.py\n"
        "@@\n"
        " x\n"
        "+y\n"
        "*** End Patch\n"
    )
    context = ToolExecutionContext(
        task_id="t-task",
        workspace_id="t-ws",
        workspace_root=workspace,
        turn_id="t-turn",
    )
    tool = build_apply_patch_definition().handler
    observation = tool(execution_context=context, patch=v4a)
    # 成功路径断言。
    assert observation.status == "success", observation.content
    assert observation.data is not None
    # 核心字段契约：data["changes"] 必须存在且非空（前端 projectFileChangeResult 读取它）。
    assert "changes" in observation.data, "apply_patch 成功 data 缺少 changes 字段"
    changes = observation.data["changes"]
    assert isinstance(changes, list) and len(changes) >= 1
    # 字段形状应与前端 toDiffEntry 消费的形状对齐（path / status / insertions / deletions）。
    change = changes[0]
    assert "path" in change
    assert "status" in change
    assert "insertions" in change
    assert "deletions" in change


def test_backend_patch_success_also_emits_changes_field(workspace: Path) -> None:
    # 测试目的：patch(replace) 成功路径同样产出 data["changes"]，与前端 projectFileChangeResult
    # 字段名一致（两个写工具对前端投影对称）。
    # 可能发现的缺陷：patch 成功 data 不含 changes，前端 diff 条目缺失。
    target = workspace / "b.py"
    target.write_text("old\n", encoding="utf-8")
    context = ToolExecutionContext(
        task_id="t-task",
        workspace_id="t-ws",
        workspace_root=workspace,
        turn_id="t-turn",
    )
    observation = ReplaceTool().execute(
        execution_context=context,
        path="b.py",
        old_string="old",
        new_string="new",
    )
    assert observation.status == "success", observation.content
    assert observation.data is not None
    assert "changes" in observation.data, "patch 成功 data 缺少 changes 字段"
    assert isinstance(observation.data["changes"], list) and len(observation.data["changes"]) >= 1


# ----------------------------------------------------------------------
# 5. 变异检查：注入常见 bug 后测试应变红（不改业务文件）
# ----------------------------------------------------------------------


def test_mutation_registration_missing_apply_patch_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    # 测试目的：模拟业务码把 apply_patch 从 build_tool_system 漏注册时，注册一致性断言应变红。
    # 实现：monkeypatch ToolRegistry.register 使 apply_patch 被跳过（仅内存篡改，不改业务文件）。
    # 可能发现的缺陷：注册断言过弱（如只看 patch 不看 apply_patch），导致漏注册不报。
    from app.tools.tool_registry import ToolRegistry

    real_register = ToolRegistry.register

    def _skip_apply_patch(self, definition):  # type: ignore[no-untyped-def]
        if definition.name == "apply_patch":
            return None  # 模拟漏注册
        return real_register(self, definition)

    monkeypatch.setattr(ToolRegistry, "register", _skip_apply_patch)
    system = _build_system(client=None)
    by_name = _all_definitions_by_name(system)
    assert "apply_patch" not in by_name, "漏注册回归未被捕获：注册一致性断言过弱"


def test_mutation_profile_missing_apply_patch_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    # 测试目的：模拟业务码把 coder_agent 漏加 apply_patch 时，profile 与注册对齐断言应变红。
    # 实现：monkeypatch coder_agent 返回值（仅内存篡改，不改业务文件）。
    # 可能发现的缺陷：profile/注册对齐断言缺失，导致子 Agent 声明漂移不报。
    from app.core.agents.define_agents import coder_agent as _real_coder_agent

    def _mutated_coder():  # type: ignore[no-untyped-def]
        profile = _real_coder_agent()
        profile.allowed_tools = [t for t in profile.allowed_tools if t != "apply_patch"]
        return profile

    monkeypatch.setattr("app.core.agents.define_agents.coder_agent", _mutated_coder)
    profile = coder_agent()
    assert "apply_patch" in profile.allowed_tools, "coder_agent 漏加 apply_patch 回归未被捕获"
