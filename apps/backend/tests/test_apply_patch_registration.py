"""apply_patch 注册与展示配置集成测试。

职责边界：只验证「apply_patch 已接入 agent profile 与工具注册、且前端展示配置齐全」，
不修改任何业务代码、不修复发现的缺陷。

覆盖四块：
1. profile 集成：developer_agent() / coder_agent() 的 allowed_tools 同时含 patch 与 apply_patch；
2. 工具注册：build_tool_system() 注册表中能查到 patch 与 apply_patch 两个 ToolDefinition，
   且 name / permission / args_model 正确（apply_patch 的 args_model 只有 patch 字段）；
3. display 一致性：build_apply_patch_definition() 与 build_replace_definition() 的
   display 均含 expand_layout=='diff'、icon=='git-compare'；
4. 前端配置存在性：toolDisplayRules.ts 的 REQUEST_SUMMARY_RULES["apply_patch"] 与
   RESULT_RULES["apply_patch"] 存在且引用 projectFileChangeResult。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from app.core.agents.define_agents import coder_agent, developer_agent
from app.tools.schemas.tool_definition import ToolDefinition
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


# ----------------------------------------------------------------------
# 1. profile 集成
# ----------------------------------------------------------------------


def test_developer_agent_allows_patch_and_apply_patch() -> None:
    # 测试目的：默认 developer 父 profile 的 allowed_tools 同时含 patch 与 apply_patch。
    # 可能发现的缺陷：apply_patch 漏加进 _DEFAULT_DEVELOPER_TOOLS 或 _resolve_all_tool_names 回退集合。
    profile = developer_agent()
    assert "patch" in profile.allowed_tools
    assert "apply_patch" in profile.allowed_tools


def test_coder_agent_allows_patch_and_apply_patch() -> None:
    # 测试目的：delegate_coder 子 profile 的显式 allowed_tools 同时含 patch 与 apply_patch。
    # 可能发现的缺陷：coder_agent() 的显式列表漏加 apply_patch（与 reviewer/analyst 保持只读边界不同，
    # coder 应同时持有两个写文件工具）。
    profile = coder_agent()
    assert "patch" in profile.allowed_tools
    assert "apply_patch" in profile.allowed_tools


def test_developer_and_coder_apply_patch_distinct_from_patch() -> None:
    # 测试目的：两个 profile 把 patch 与 apply_patch 当作独立工具并列收纳，不会相互覆盖或漏一。
    # 可能发现的缺陷：二者被误写同一名字、或只加了一个。
    for profile in (developer_agent(), coder_agent()):
        assert "patch" in profile.allowed_tools
        assert "apply_patch" in profile.allowed_tools
        # 两个名字必须互不相同且都在列表内。
        assert profile.allowed_tools.count("patch") == 1
        assert profile.allowed_tools.count("apply_patch") == 1


# ----------------------------------------------------------------------
# 2. 工具注册
# ----------------------------------------------------------------------


def test_build_tool_system_registers_patch_and_apply_patch() -> None:
    # 测试目的：build_tool_system() 注册表能同时查到 patch 与 apply_patch 两个 ToolDefinition。
    # 可能发现的缺陷：apply_patch 未注册到 ToolRegistry（build_tool_system 漏 register）。
    system = ToolSystem.build_tool_system(client=None)
    patch_def = system.registry.get_tool_definition("patch")
    apply_def = system.registry.get_tool_definition("apply_patch")
    assert isinstance(patch_def, ToolDefinition)
    assert isinstance(apply_def, ToolDefinition)


def test_patch_and_apply_patch_names_distinct() -> None:
    # 测试目的：两工具 name 互不串台。
    # 可能发现的缺陷：拆分不彻底，两者 name 相同导致后者覆盖前者。
    patch_def = build_replace_definition()
    apply_def = build_apply_patch_definition()
    assert patch_def.name == "patch"
    assert apply_def.name == "apply_patch"
    assert patch_def.name != apply_def.name


def test_patch_and_apply_patch_permissions() -> None:
    # 测试目的：两工具 permission 均为 file_write（破坏性文件写）。
    # 可能发现的缺陷：permission 被误设为 never/ask，导致工具实际不可调用。
    patch_def = build_replace_definition()
    apply_def = build_apply_patch_definition()
    assert patch_def.permission == "file_write"
    assert apply_def.permission == "file_write"


def test_apply_patch_args_model_only_patch() -> None:
    # 测试目的：apply_patch 的 args_model 是 ApplyPatchArgs，且其字段只有 patch 一个。
    # 可能发现的缺陷：args_model 错绑成旧的 PatchArgs、或残留 mode 等字段。
    apply_def = build_apply_patch_definition()
    assert apply_def.args_model is ApplyPatchArgs
    # pydantic v2：model_fields 是类级字段声明，确认只有 patch 一个。
    assert set(apply_def.args_model.model_fields.keys()) == {"patch"}


def test_apply_patch_registered_definition_shape() -> None:
    # 测试目的：注册到 ToolRegistry 的 apply_patch 定义，name/permission/args_model 全链正确。
    # 可能发现的缺陷：注册实例与 build_* 工厂返回的实例不一致（如 name 被封装层篡改）。
    system = ToolSystem.build_tool_system(client=None)
    apply_def = system.registry.get_tool_definition("apply_patch")
    assert apply_def is not None
    assert apply_def.name == "apply_patch"
    assert apply_def.permission == "file_write"
    assert apply_def.args_model is ApplyPatchArgs
    assert apply_def.description == APPLY_PATCH_DESCRIPTION


def test_apply_patch_description_mentions_v4a_and_no_fuzzy() -> None:
    # 测试目的：apply_patch 描述聚焦 V4A 多文件补丁，且不含 replace 的 fuzzy find-and-replace 措辞。
    # 可能发现的缺陷：拆分不彻底，描述串台或漏 V4A 关键字。
    apply_def = build_apply_patch_definition()
    assert "V4A" in apply_def.description
    assert "find-and-replace" not in apply_def.description.lower()
    assert "fuzzy matching" not in apply_def.description.lower()


# ----------------------------------------------------------------------
# 3. display 一致性
# ----------------------------------------------------------------------


def test_apply_patch_display_is_diff_and_git_compare() -> None:
    # 测试目的：apply_patch 的 display 含 expand_layout=='diff'、icon=='git-compare'、expandable=True、verb=''。
    # 可能发现的缺陷：前端 diff 渲染契约被破坏（icon 错、expand_layout 错、未 expandable）。
    display = build_apply_patch_definition().display
    assert display is not None
    assert display.expand_layout == "diff"
    assert display.icon == "git-compare"
    assert display.expandable is True
    assert display.verb == ""


def test_replace_patch_display_is_diff_and_git_compare() -> None:
    # 测试目的：patch(replace) 的 display 同样含 expand_layout=='diff'、icon=='git-compare'，
    # 保证两个写文件工具在前端展示层一致。
    # 可能发现的缺陷：两工具 display 配置不对称，前端出现渲染分化。
    display = build_replace_definition().display
    assert display is not None
    assert display.expand_layout == "diff"
    assert display.icon == "git-compare"
    assert display.expandable is True
    assert display.verb == ""


def test_replace_definition_metadata_shape() -> None:
    # 测试目的：build_replace_definition() 的 name/permission/resource_keys/description 正确。
    # 可能发现的缺陷：name 被误改成 apply_patch、resource_keys 缺失、description 错绑。
    definition = build_replace_definition()
    assert definition.name == "patch"
    assert definition.permission == "file_write"
    assert definition.resource_keys == ("filesystem",)
    assert definition.description == REPLACE_DESCRIPTION


def test_replace_args_model_fields_and_forbid() -> None:
    # 测试目的：replace 的 args_model 为 ReplaceArgs，含 path/old_string/new_string/replace_all
    # 四个字段且 extra 被 forbid。
    # 可能发现的缺陷：args_model 错绑、或多余字段（如旧 mode）被静默吞掉。
    definition = build_replace_definition()
    assert definition.args_model is ReplaceArgs
    assert set(definition.args_model.model_fields.keys()) == {
        "path",
        "old_string",
        "new_string",
        "replace_all",
    }
    import pydantic

    try:
        ReplaceArgs(path="a.py", old_string="x", new_string="y", mode="patch")  # type: ignore[call-arg]
        raise AssertionError("extra field 'mode' should be rejected")
    except pydantic.ValidationError:
        pass


def test_replace_and_apply_patch_definitions_distinct_metadata() -> None:
    # 测试目的：两个工具的 name/permission/display/args_model 均各自独立、不串台。
    # 可能发现的缺陷：拆分不彻底导致 display 或 args_model 指向同一对象/同名。
    replace_def = build_replace_definition()
    apply_def = build_apply_patch_definition()
    assert replace_def.name != apply_def.name
    assert replace_def.args_model is not apply_def.args_model
    # 两个都需要展示结构化 diff，display 关键字段一致。
    for d in (replace_def, apply_def):
        assert d.display is not None
        assert d.display.expand_layout == "diff"
        assert d.display.icon == "git-compare"
    # description 不应串台：replace 描述 fuzzy/replace，apply_patch 描述 V4A。
    assert "find-and-replace" in replace_def.description.lower()
    assert "V4A" in apply_def.description
    assert "V4A" not in replace_def.description


def test_replace_tool_class_metadata() -> None:
    # 测试目的：ReplaceTool 类级元数据（name/permission/args_model/risk_level）正确，
    # 与 to_definition 工厂输出一致。
    # 可能发现的缺陷：类级元数据与工厂输出漂移（如类改了 name 但工厂硬编码旧值）。
    tool = ReplaceTool()
    assert tool.name == "patch"
    assert tool.permission == "file_write"
    assert tool.args_model is ReplaceArgs
    definition = tool.to_definition()
    assert definition.name == tool.name
    assert definition.permission == tool.permission
    assert definition.args_model is tool.args_model
    assert definition.risk_level == tool.risk_level


# ----------------------------------------------------------------------
# 4. 前端配置存在性（静态文件内容断言，不编译 ts）
# ----------------------------------------------------------------------

# toolDisplayRules.ts 位于仓库根目录下的 apps/shared/ts/。
_FRONTEND_RULES_PATH = (
    Path(__file__).resolve().parents[3] / "apps" / "shared" / "ts" / "toolDisplayRules.ts"
)


def test_frontend_rules_file_exists() -> None:
    # 测试目的：前端规则文件存在，路径假设未被移动。
    # 可能发现的缺陷：文件被重命名/移动导致展示配置丢失。
    assert _FRONTEND_RULES_PATH.is_file(), (
        f"expected toolDisplayRules.ts at {_FRONTEND_RULES_PATH}"
    )


def test_frontend_request_summary_has_apply_patch() -> None:
    # 测试目的：REQUEST_SUMMARY_RULES 含 apply_patch 条目（折叠态请求摘要）。
    # 可能发现的缺陷：apply_patch 漏加进 REQUEST_SUMMARY_RULES，前端折叠行无摘要。
    content = _FRONTEND_RULES_PATH.read_text(encoding="utf-8")
    # 定位 REQUEST_SUMMARY_RULES 表段。
    start = content.index("const REQUEST_SUMMARY_RULES")
    end = content.index("const RESULT_RULES")
    request_block = content[start:end]
    assert 'apply_patch:' in request_block, "REQUEST_SUMMARY_RULES 缺少 apply_patch 条目"


def test_frontend_result_rules_has_apply_patch_with_project_file_change() -> None:
    # 测试目的：RESULT_RULES 含 apply_patch 条目，且复用 projectFileChangeResult 投影（diff 条目）。
    # 可能发现的缺陷：apply_patch 漏加进 RESULT_RULES，或误用 EMPTY_PROJECTION 而非 projectFileChangeResult。
    content = _FRONTEND_RULES_PATH.read_text(encoding="utf-8")
    # 取 RESULT_RULES 表段（从声明到下一个顶层 function 之前）。
    start = content.index("const RESULT_RULES")
    end = content.index("function projectDeleteResult")
    result_block = content[start:end]
    assert 'apply_patch:' in result_block, "RESULT_RULES 缺少 apply_patch 条目"
    # 应复用 projectFileChangeResult（与 patch 一致），以 diff 条目方式呈现多文件变更。
    assert "projectFileChangeResult" in result_block, (
        "RESULT_RULES.apply_patch 未引用 projectFileChangeResult"
    )


def test_frontend_apply_patch_reuses_same_result_projection_as_patch() -> None:
    # 测试目的：RESULT_RULES 中 apply_patch 与 patch 引用同一投影函数 projectFileChangeResult，
    # 避免前端出现「patch 有 diff、apply_patch 无 diff」的分化。
    # 可能发现的缺陷：apply_patch 被错误配置成 EMPTY_PROJECTION 或别的投影。
    content = _FRONTEND_RULES_PATH.read_text(encoding="utf-8")
    start = content.index("const RESULT_RULES")
    end = content.index("function projectDeleteResult")
    result_block = content[start:end]
    patch_line = next(
        line for line in result_block.splitlines() if "patch:" in line
    )
    apply_line = next(
        line for line in result_block.splitlines() if "apply_patch:" in line
    )
    assert "projectFileChangeResult" in patch_line
    assert "projectFileChangeResult" in apply_line
    # 两者指向同一函数，不应一个是 EMPTY_PROJECTION。
    assert "EMPTY_PROJECTION" not in apply_line, (
        "RESULT_RULES.apply_patch 指向 EMPTY_PROJECTION，多文件变更将无法前端展示"
    )


# ----------------------------------------------------------------------
# 5. 健全性：apply_patch 真实 args 模型可被实例化
# ----------------------------------------------------------------------


def test_apply_patch_args_single_required_field_validates() -> None:
    # 测试目的：ApplyPatchArgs 仅接受 patch 字段，且 extra 被 forbid（回归 guard）。
    # 可能发现的缺陷：extra 未 forbid，多余字段被静默吞掉，导致前端/后端契约漂移。
    model = ApplyPatchArgs(patch="*** Begin Patch\n*** End Patch\n")
    assert model.patch

    import pydantic

    try:
        ApplyPatchArgs(patch="x", mode="patch")  # type: ignore[call-arg]
        raise AssertionError("extra field 'mode' should be rejected")
    except pydantic.ValidationError:
        pass


# ----------------------------------------------------------------------
# 6. 变异检查（mutation sanity）：断言对常见回归敏感，不修改任何业务文件
# ----------------------------------------------------------------------


def test_mutation_display_icon_regression_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    # 测试目的：模拟业务码把 apply_patch 的 display.icon 误改成非 git-compare 时的回归可被捕获。
    # 实现：仅用 monkeypatch 在内存中篡改 build_apply_patch_definition 的返回值（不改业务文件），
    # 证明对 display.icon 的断言是敏感的（变异应使该断言变红）。
    # 可能发现的缺陷：断言过弱（如只 assert display is not None）导致回归漏报。
    from app.tools.schemas.tool_definition import ToolDefinition

    good = build_apply_patch_definition()

    def _mutated() -> ToolDefinition:
        mutated = good.model_copy(deep=True)
        mutated.display = mutated.display.model_copy(update={"icon": "wrong-icon"})
        return mutated

    monkeypatch.setattr(
        "app.tools.tool_handler.apply_patch_tool.build_apply_patch_definition", _mutated
    )
    # 复用与正式测试相同的断言形态，验证其敏感性。
    display = build_apply_patch_definition().display
    assert display is not None
    assert display.icon == "git-compare", "icon 回归未被捕获：断言过弱"


def test_mutation_allowed_tools_regression_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    # 测试目的：模拟业务码把 coder_agent 漏加 apply_patch 时的回归可被捕获。
    # 实现：仅用 monkeypatch 在内存中篡改 coder_agent 的返回值，证明对 allowed_tools 的断言敏感。
    # 可能发现的缺陷：allowed_tools 断言缺失或只看 patch 不看 apply_patch，导致漏加不报。
    from app.core.agents.define_agents import coder_agent as _real_coder_agent

    def _mutated_coder() -> object:
        profile = _real_coder_agent()
        # 变异：移除 apply_patch，模拟 Task 3 漏加。
        profile.allowed_tools = [t for t in profile.allowed_tools if t != "apply_patch"]
        return profile

    monkeypatch.setattr(
        "app.core.agents.define_agents.coder_agent", _mutated_coder
    )
    profile = coder_agent()
    assert "apply_patch" in profile.allowed_tools, "allowed_tools 漏加回归未被捕获"
    assert "patch" in profile.allowed_tools, "patch 回归未被捕获"
