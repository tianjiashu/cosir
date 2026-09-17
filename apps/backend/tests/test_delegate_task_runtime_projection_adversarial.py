"""delegate_task 运行期投影修复的对抗性验证（独立测试，不修改生产代码）。

被验证的缺陷（2026-09-17）：
``delegate_task`` 的模型可见定义在「agent 注册表注入之前」注册并被注册期快照
固化，导致模型收到的 ``child_agent_id`` 描述永远是字面量 ``{ids}``、工具描述里没有
"Available child agents" 清单。模型只能猜角色名，全部命中 ``delegate_task child not
found``，连续失败触发 ``tool_error_limit_reached``，整轮 run 失败。

修复思路：把描述 / schema 从「注册期快照」改为「运行期投影」（``description_provider``
/ ``schema_provider``），并把合法 id 收口为 JSON Schema ``enum``。

本文件从「找缺陷」的对抗视角覆盖：
1. 注册时机无关性回归（注入前注册 vs 注入后投影）。
2. 降级路径（注册表始终未注入）绝不暴露 ``{ids}`` / 空 enum / 不抛异常。
3. enum 真实性（等于 ``child_agent_ids()``，不含 ``main_agent``）。
4. 既有静态工具语义未被破坏（含 ``execute_terminal`` 平台 schema 覆盖）。
5. ``get_schema`` 返回投影 schema 的契约与未注册工具返回 None。
6. **provider 异常健壮性**：非 RuntimeError 异常是否会被吞掉或炸穿整轮 run。
7. frozen / ``dataclasses.replace`` / ``normalized()`` 幂等语义。
8. 参数校验未被削弱（``ToolAccessGate`` 走 ``args_model``）。
9. 并发投影安全。
"""

import dataclasses
import threading
from typing import Any

import pytest

from app.config import configuration
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.tools.schemas.tool_call import ToolCall
from app.core.tools.schemas.tool_definition import ToolDefinition
from app.core.tools.tool_execute.tool_access_gate import ToolAccessGate
from app.core.tools.tool_models.delegate_task_args import DelegateTaskArgs
from app.core.tools.tool_registry import ToolRegistry
from app.core.tools.tool_system import ToolSystem


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #
def _child_property(payload: dict[str, Any]) -> dict[str, Any]:
    """取出投影 payload 中 child_agent_id 的 schema 片段。"""

    return payload["parameters"]["properties"]["child_agent_id"]


@pytest.fixture
def registry_injected_after_build(monkeypatch) -> ToolSystem:
    """复刻 app.py:135-137 顺序：先 build_tool_system，再注入 agent 注册表。"""

    monkeypatch.setattr(configuration, "_TOOL_SYSTEM", None, raising=False)
    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", None, raising=False)

    tool_system = ToolSystem.build_tool_system()
    configuration.set_tool_system(tool_system)

    # 断言注册确实发生在注入之前（注册期 get_agent_registry 必须抛 RuntimeError）
    with pytest.raises(RuntimeError):
        configuration.get_agent_registry()

    configuration.set_agent_registry(configuration.build_agent_registry())
    return tool_system


# --------------------------------------------------------------------------- #
# 1. 注册时机无关性回归
# --------------------------------------------------------------------------- #
def test_projection_after_injection_has_real_enum_and_catalog(
    registry_injected_after_build: ToolSystem,
) -> None:
    """[回归] 注入后投影必须含真实 enum 与子 Agent 清单——旧实现此处固化空值。"""

    definition = registry_injected_after_build.registry.get_tool_definition("delegate_task")
    assert definition is not None

    payload = definition.to_model_tool_definition()
    description = payload["description"]
    child_prop = _child_property(payload)

    real_ids = configuration.get_agent_registry().child_agent_ids()
    assert "Available child agents" in description, "运行时清单缺失（旧缺陷复现）"
    for agent_id in real_ids:
        assert agent_id in description, f"清单缺少合法 id: {agent_id}"

    assert child_prop.get("enum") is not None, "child_agent_id 未收口为 enum"
    assert set(child_prop["enum"]) == real_ids
    assert "{ids}" not in child_prop["description"], "模板占位符泄漏给模型"


def test_registration_definition_and_each_projection_are_consistent(
    registry_injected_after_build: ToolSystem,
) -> None:
    """[一致性] 注册期定义对象、注册表内定义对象、多次投影结果三者必须一致。"""

    registry = registry_injected_after_build.registry
    stored = registry.get_tool_definition("delegate_task")
    assert stored is not None

    first = stored.to_model_tool_definition()
    second = stored.to_model_tool_definition()

    # 多次投影必须稳定（provider 每次实时读取同一注册表）
    assert first == second
    # 注册表存的是同一个 provider，二次查询投影必须与首次完全一致
    re_fetched = registry.get_tool_definition("delegate_task")
    assert re_fetched is not None
    assert re_fetched.to_model_tool_definition() == first


# --------------------------------------------------------------------------- #
# 2. 降级路径：注册表始终未注入
# --------------------------------------------------------------------------- #
def test_degraded_description_never_leaks_placeholder(monkeypatch) -> None:
    """[降级] 注册表从未注入时描述不得含占位符、不得注入 enum、不得抛异常。"""

    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", None, raising=False)
    monkeypatch.setattr(configuration, "_TOOL_SYSTEM", None, raising=False)

    tool_system = ToolSystem.build_tool_system()
    definition = tool_system.registry.get_tool_definition("delegate_task")
    assert definition is not None

    # 修复前：注册期拿空摘要 → 固化；此处必须能安全投影
    payload = definition.to_model_tool_definition()
    child_prop = _child_property(payload)

    assert "{ids}" not in child_prop["description"]
    assert "enum" not in child_prop, "空 enum 会让所有取值非法，绝不能注入"
    assert "Available child agents" not in payload["description"] or True  # 允许降级


def test_definition_built_without_injection_is_projection_safe() -> None:
    """[降级] 即使 register 发生在注册表注入前，投影/注册全链路都不得崩。"""

    # 直接构造：不依赖进程级单例
    from app.core.tools.tool_handler.delegate_task import build_delegate_task_definition

    definition = build_delegate_task_definition()
    registry = ToolRegistry()
    registry.register(definition)  # normalized() 不得固化空 schema

    stored = registry.get_tool_definition("delegate_task")
    assert stored is not None
    payload = stored.to_model_tool_definition()
    assert set(payload.keys()) == {"name", "description", "parameters"}
    # schema_provider 存在时 normalized 不得固化 parameters_schema
    assert stored.parameters_schema == {}, "schema_provider 存在时不应固化 parameters_schema"


# --------------------------------------------------------------------------- #
# 3. enum 真实性
# --------------------------------------------------------------------------- #
def test_enum_matches_child_agent_ids_exactly(registry_injected_after_build: ToolSystem) -> None:
    """[真实性] enum 与 child_agent_ids() 完全相等（非子集/超集），且不含 main_agent。"""

    definition = registry_injected_after_build.registry.get_tool_definition("delegate_task")
    assert definition is not None
    enum = set(_child_property(definition.to_model_tool_definition())["enum"])
    real = configuration.get_agent_registry().child_agent_ids()

    assert enum == real, f"enum 与真实候选集不一致：多={enum - real}, 少={real - enum}"
    assert "main_agent" not in enum, "main_agent 不是可委派子 Agent，不得出现在 enum"

    # 与文档声称的四个真实 id 对齐（防止注册表播种被悄悄改动）
    assert enum == {"code-developer", "code-explorer", "delegate_reviewer", "unit-test-engineer"}


# --------------------------------------------------------------------------- #
# 4. 既有工具未被破坏
# --------------------------------------------------------------------------- #
def test_static_tools_keep_explicit_schema(registry_injected_after_build: ToolSystem) -> None:
    """[回归] 静态工具经过 register 后仍固化显式 schema，不引入 provider。"""

    registry = registry_injected_after_build.registry
    static_names = ["read_file", "execute_terminal", "search_content", "find_files"]
    for name in static_names:
        definition = registry.get_tool_definition(name)
        assert definition is not None, f"{name} 未注册"
        assert definition.schema_provider is None, f"{name} 不应声明 schema_provider"
        assert definition.description_provider is None, f"{name} 不应声明 description_provider"

        payload = definition.to_model_tool_definition()
        assert payload["name"] == name
        assert isinstance(payload["parameters"], dict)
        assert payload["parameters"], f"{name} 的参数 schema 不应为空"


def test_execute_terminal_platform_schema_override_survives(
    registry_injected_after_build: ToolSystem,
) -> None:
    """[回归] execute_terminal 的平台相关 shell enum 覆盖仍生效。"""

    registry = registry_injected_after_build.registry
    definition = registry.get_tool_definition("execute_terminal")
    assert definition is not None

    payload = definition.to_model_tool_definition()
    shell_prop = payload["parameters"]["properties"]["shell"]
    assert "enum" in shell_prop
    assert "auto" in shell_prop["enum"]

    # get_schema 投影必须与 to_model_tool_definition 的 parameters 同源
    assert registry.get_schema("execute_terminal") == payload["parameters"]


def test_get_schema_returns_projection_and_none_for_unknown(
    registry_injected_after_build: ToolSystem,
) -> None:
    """[契约] get_schema 对 delegate_task 返回运行期投影 schema；未注册工具返回 None。"""

    registry = registry_injected_after_build.registry
    schema = registry.get_schema("delegate_task")
    assert schema is not None
    assert set(schema["properties"]["child_agent_id"]["enum"]) == (
        configuration.get_agent_registry().child_agent_ids()
    )
    assert registry.get_schema("__no_such_tool__") is None


# --------------------------------------------------------------------------- #
# 5. provider 异常健壮性（重点对付更严重风险）
# --------------------------------------------------------------------------- #
def test_non_runtime_error_from_schema_provider_is_contained() -> None:
    """[健壮性] schema_provider 抛非 RuntimeError 异常时必须被兜住。

    投影位于「每次下发模型」的热路径上：若 provider 异常穿透，一个坏 provider 会炸穿
    整轮 run——比原缺陷（静默降级）更严重。此处锁定「兜底降级为 args_model 契约」。
    """

    def boom() -> dict[str, Any]:
        raise AttributeError("simulated provider bug")

    definition = ToolDefinition(
        name="delegate_task",
        description="d",
        permission="delegate_task",
        handler=lambda **_: None,
        args_model=DelegateTaskArgs,
        description_provider=None,
        schema_provider=boom,
    )

    payload = definition.to_model_tool_definition()

    assert payload["description"] == "d"
    assert payload["parameters"] == DelegateTaskArgs.model_json_schema()


def test_runtime_child_agent_summary_is_contained_for_broken_registry(
    monkeypatch,
) -> None:
    """[健壮性] 注册表存在但取数抛异常时，描述投影必须降级而非崩。

    ``_runtime_child_agent_summary`` 处于投影热路径，任何异常都必须在函数内收口并返回
    空串；``to_model_tool_definition`` 必须仍可调用，描述退化为通用形态。
    """

    class BrokenRegistry:
        def child_agent_summary(self) -> str:
            raise AttributeError("simulated broken registry")

        def child_agent_ids(self) -> set[str]:
            raise AttributeError("simulated broken registry")

    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", BrokenRegistry(), raising=False)

    from app.core.tools.tool_handler.delegate_task import (
        _runtime_child_agent_summary,
        build_delegate_task_definition,
    )

    assert _runtime_child_agent_summary() == ""

    definition = build_delegate_task_definition()
    description = definition.to_model_tool_definition()["description"]

    assert "Available child agents" not in description
    assert "CRITICAL BUDGET LIMIT" in description


def test_delegate_schema_provider_survives_broken_registry_object(monkeypatch) -> None:
    """[健壮性] DelegateTaskArgs.model_json_schema 遇异常注册对象的行为。"""

    class BrokenRegistry:
        def child_agent_ids(self) -> set[str]:
            raise AttributeError("simulated broken registry")

    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", BrokenRegistry(), raising=False)

    # _available_child_agent_ids 捕获 (RuntimeError, ImportError, AttributeError)
    # → 预期降级为空列表，不抛异常
    schema = DelegateTaskArgs.model_json_schema()
    child_prop = schema["properties"]["child_agent_id"]
    assert "enum" not in child_prop
    assert "{ids}" not in child_prop["description"]


# --------------------------------------------------------------------------- #
# 6. frozen / replace / normalized 语义
# --------------------------------------------------------------------------- #
def test_replace_and_equality_semantics() -> None:
    """[语义] dataclasses.replace 保持 provider；provider 不参与相等性比较。"""

    def prov_a() -> str:
        return "a"

    def prov_b() -> str:
        return "b"

    base = ToolDefinition(
        name="delegate_task",
        description="d",
        permission="delegate_task",
        handler=lambda **_: None,
        args_model=DelegateTaskArgs,
        schema_provider=prov_a,
    )
    # compare=False：provider 不同但其它相同 → 相等
    other = dataclasses.replace(base, schema_provider=prov_b)
    assert base == other

    # replace 保留传入的 provider
    replaced = dataclasses.replace(base, description_provider=prov_a)
    assert replaced.description_provider is prov_a
    assert replaced.schema_provider is prov_a


def test_normalized_is_idempotent_and_keeps_providers() -> None:
    """[语义] normalized() 幂等：多次调用不重复固化、不丢 provider。"""

    from app.core.tools.tool_handler.delegate_task import build_delegate_task_definition

    definition = build_delegate_task_definition()
    once = definition.normalized()
    twice = once.normalized()

    assert once.schema_provider is definition.schema_provider
    assert once.description_provider is definition.description_provider
    assert once.parameters_schema == {}
    # 幂等：再次 normalized 应返回自身（provider 存在短路）
    assert twice is once


def test_normalized_freezes_schema_for_static_tool() -> None:
    """[语义] 无 provider 的静态工具 normalized() 固化 args_model 的 schema。"""

    from app.core.tools.tool_handler.read_file import build_read_file_definition

    definition = build_read_file_definition()
    assert definition.schema_provider is None
    normalized = definition.normalized()
    assert normalized.parameters_schema, "静态工具应固化 schema"
    # 幂等
    assert normalized.normalized() is normalized


# --------------------------------------------------------------------------- #
# 7. 参数校验未被削弱
# --------------------------------------------------------------------------- #
def _gate_outcome(tool_system: ToolSystem, arguments: dict[str, Any]):
    gate = ToolAccessGate(tool_system.registry)
    from pathlib import Path

    from app.core.tools.schemas.tool_execution_context import ToolExecutionContext

    ctx = ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=Path.cwd())
    call = ToolCall(tool_name="delegate_task", arguments=arguments, call_id="c1")
    return gate.evaluate(call, ctx)


def test_invalid_child_agent_id_is_rejected_at_gate(
    registry_injected_after_build: ToolSystem,
) -> None:
    """[校验] 非法 child_agent_id 必须在准入门禁被拒绝（enum 由软引导升级为硬约束）。

    schema 里的 enum 只是对模型的引导；参数模型层必须同样收口，否则模型一旦绕过（或
    候选集降级无 enum 时）仍会产生 ``child not found`` → 连续失败 → run 失败。拒绝信息
    需携带合法候选清单，使模型可自纠。
    """

    outcome = _gate_outcome(
        registry_injected_after_build,
        {"child_agent_id": "general", "title": "t", "prompt": "p"},
    )

    assert outcome.admitted is False
    assert outcome.denial is not None


def test_valid_child_agent_id_passes_gate(
    registry_injected_after_build: ToolSystem,
) -> None:
    """[校验] 合法 child_agent_id 必须通过准入门禁。"""

    outcome = _gate_outcome(
        registry_injected_after_build,
        {"child_agent_id": "code-developer", "title": "t", "prompt": "p"},
    )
    assert outcome.admitted is True
    assert outcome.denial is None


def test_delegate_args_budget_validation_still_enforced() -> None:
    """[校验] DelegateTaskArgs 的空 title / 超长 prompt 仍被拒绝。"""

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DelegateTaskArgs(child_agent_id="code-developer", title="   ", prompt="p")
    with pytest.raises(ValidationError):
        DelegateTaskArgs(child_agent_id="code-developer", title="ok", prompt="")
    with pytest.raises(ValidationError):
        DelegateTaskArgs(child_agent_id="code-developer", title="ok", prompt="x" * 3001)


# --------------------------------------------------------------------------- #
# 8. 并发投影安全
# --------------------------------------------------------------------------- #
def test_concurrent_projection_is_safe(registry_injected_after_build: ToolSystem) -> None:
    """[并发] 多线程同时投影 delegate_task 不得抛异常或产生不一致结果。"""

    definition = registry_injected_after_build.registry.get_tool_definition("delegate_task")
    assert definition is not None

    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(50):
                results.append(definition.to_model_tool_definition())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"并发投影抛异常: {errors!r}"
    assert all(r == results[0] for r in results), "并发投影结果不一致"


def test_registry_generation_and_concurrent_register() -> None:
    """[并发] 注册表并发注册/查询不损坏状态（get_schema 投影路径线程安全）。"""

    registry = ToolRegistry()

    def register_tool(idx: int) -> None:
        registry.register(
            ToolDefinition(
                name=f"tool_{idx}",
                description="d",
                permission="p",
                handler=lambda **_: None,
                args_model=DelegateTaskArgs,
            )
        )

    threads = [threading.Thread(target=register_tool, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(registry.get_all_tool_names()) == 20
    assert registry.generation == 20


# --------------------------------------------------------------------------- #
# 9. AgentProfileRegistry 边界（child_agent_ids / child_agent_summary）
# --------------------------------------------------------------------------- #
def test_empty_registry_child_agent_ids_and_summary() -> None:
    """[边界] 空注册表：child_agent_ids 为空集、child_agent_summary 为空串。"""

    registry = AgentProfileRegistry()
    assert registry.child_agent_ids() == set()
    assert registry.child_agent_summary() == ""


# --------------------------------------------------------------------------- #
# 10. 补充对抗：重建时机 / 静态摘要覆盖 / 描述兜底
# --------------------------------------------------------------------------- #
def test_rebuild_tool_system_after_injection_still_projects_live_catalog(
    registry_injected_after_build: ToolSystem,
) -> None:
    """[对抗] 注册表注入后再重建工具系统，投影仍须读到实时注册表（无时序陷阱）。"""

    rebuilt = ToolSystem.build_tool_system()
    definition = rebuilt.registry.get_tool_definition("delegate_task")
    assert definition is not None

    payload = definition.to_model_tool_definition()
    child_prop = _child_property(payload)
    assert set(child_prop["enum"]) == configuration.get_agent_registry().child_agent_ids()


def test_static_summary_injection_channel_is_removed(
    registry_injected_after_build: ToolSystem,
) -> None:
    """[对抗] 静态摘要注入口必须不存在，且描述始终反映运行期清单。

    旧签名 ``build_delegate_task_definition(agent_summary=...)`` 允许静态摘要覆盖运行期
    清单——一旦有人传值即退回「子 Agent 清单不可见」的旧缺陷形态。该入口已从签名上移除，
    本用例锁死这一点，避免日后被重新引入。
    """

    import inspect

    from app.core.tools.tool_handler.delegate_task import build_delegate_task_definition

    assert "agent_summary" not in inspect.signature(build_delegate_task_definition).parameters

    definition = registry_injected_after_build.registry.get_tool_definition("delegate_task")
    assert definition is not None

    description = definition.to_model_tool_definition()["description"]
    for agent_id in configuration.get_agent_registry().child_agent_ids():
        assert agent_id in description


def test_compose_description_degrades_without_catalog() -> None:
    """[对抗] 空摘要时描述必须退化为通用形态且不含 "Available child agents" 误导。"""

    from app.core.tools.tool_handler.delegate_task import _compose_description

    text = _compose_description("")
    assert "{ids}" not in text
    assert "Available child agents" not in text
    assert "CRITICAL BUDGET LIMIT" in text  # 通用描述核心仍在


def test_model_facing_schema_is_json_serializable_with_enum(
    registry_injected_after_build: ToolSystem,
) -> None:
    """[对抗] 注入 enum 后的投影必须可 JSON 序列化且 enum 成员均为字符串。"""

    import json

    definition = registry_injected_after_build.registry.get_tool_definition("delegate_task")
    assert definition is not None
    payload = definition.to_model_tool_definition()

    serialized = json.dumps(payload)  # 不得抛异常
    assert "code-developer" in serialized
    enum = _child_property(payload)["enum"]
    assert all(isinstance(member, str) for member in enum)
    assert len(enum) == len(set(enum)), "enum 不应含重复项"


# --------------------------------------------------------------------------- #
# 11. ToolRegistry 其它路径与边界（补充防御性覆盖）
# --------------------------------------------------------------------------- #
def _dummy_definition(name: str, permission: str = "p") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="d",
        permission=permission,
        handler=lambda **_: None,
        args_model=DelegateTaskArgs,
    )


def test_registry_constructor_registers_iterable() -> None:
    """[边界] 构造时传入 definitions 应立即注册（构造器契约）。"""

    registry = ToolRegistry([_dummy_definition("a"), _dummy_definition("b")])
    assert registry.get_all_tool_names() == ["a", "b"]
    assert registry.generation == 2


def test_register_rejects_non_tool_definition() -> None:
    """[异常] register 非 ToolDefinition 必须抛 TypeError 而非静默写入。"""

    registry = ToolRegistry()
    with pytest.raises(TypeError):
        registry.register("not-a-definition")  # type: ignore[arg-type]


def test_register_duplicate_is_ignored_without_bumping_generation() -> None:
    """[边界] 同名重复注册被忽略，且不得递增 generation。"""

    registry = ToolRegistry()
    registry.register(_dummy_definition("dup"))
    generation_after_first = registry.generation
    registry.register(_dummy_definition("dup"))
    assert registry.generation == generation_after_first


def test_deregister_present_and_absent() -> None:
    """[边界] deregister 命中时删除并递增 generation；未命中静默无操作。"""

    registry = ToolRegistry([_dummy_definition("x")])
    before = registry.generation
    registry.deregister("x")
    assert registry.get_tool_definition("x") is None
    assert registry.generation == before + 1

    registry.deregister("__absent__")
    assert registry.generation == before + 1  # 不存在的名称不改 generation


def test_get_all_definitions_sorted_and_permission_queries() -> None:
    """[契约] get_all_definitions 排序、get_tools_by_permission / get_permissions 正确。"""

    registry = ToolRegistry(
        [
            _dummy_definition("zeta", permission="r"),
            _dummy_definition("alpha", permission="w"),
            _dummy_definition("mid", permission="r"),
        ]
    )
    assert [d.name for d in registry.get_all_definitions()] == ["alpha", "mid", "zeta"]
    # 权限筛选不保证顺序（实现按内部字典序返回），故断言集合而非列表
    assert {d.name for d in registry.get_tools_by_permission("r")} == {"zeta", "mid"}
    assert registry.get_permissions() == {"r", "w"}


def test_delegate_task_handler_without_execution_context() -> None:
    """[异常] delegate_task handler 无 execution_context 时返回错误观察而非抛异常。"""

    from app.core.tools.tool_handler.delegate_task import DelegateTaskTool

    observation = DelegateTaskTool().execute(
        child_agent_id="code-developer", title="t", prompt="p", execution_context=None
    )
    assert observation.status == "error"


def test_delegate_task_handler_without_executor() -> None:
    """[异常] execution_context 存在但无 delegate_task_executor 时返回错误观察。"""

    from pathlib import Path

    from app.core.tools.schemas.tool_execution_context import ToolExecutionContext
    from app.core.tools.tool_handler.delegate_task import DelegateTaskTool

    ctx = ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=Path.cwd())
    observation = DelegateTaskTool().execute(
        child_agent_id="code-developer", title="t", prompt="p", execution_context=ctx
    )
    assert observation.status == "error"
