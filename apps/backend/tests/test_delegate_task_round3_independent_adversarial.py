"""第三轮独立对抗验证：delegate_task 三项修复的真伪复核（不修改生产代码）。

被验证的修复（来源：修复者自述，本文件独立核实，不依赖其描述）：
  修复 1 ``ToolDefinition.project_description`` / ``project_parameters``：provider 异常
      兜底（描述降级静态值、schema 降级 ``args_model.model_json_schema()``）+ WARNING。
  修复 2 ``DelegateTaskArgs._validate_child_agent_id``：候选集可用时硬拒非法 id、
      候秀集不可用时放行，且 ``_available_child_agent_ids`` 捕获范围扩到 ``Exception``。
  修复 3 ``build_delegate_task_definition``：移除 ``agent_summary`` 静态注入口。

本文件聚焦「用我自己的新用例独立复核」以下对抗点：
  A. 修复 1 的契约细节：静态 schema 真的优先？日志事件键/字段真的写出？
     ``BaseException`` 子类（``KeyboardInterrupt`` / ``SystemExit``）是否按契约**不被吞**？
     ``MemoryError``（Exception 子类）是否被兜底？
  B. 修复 2 是否引入新破坏：候选集可用时合法 id 不被误拒、候选不可用时放行、
     失败消息含候选清单；空白串/超长串/非字符串/bytes 边界。
  C. 投影失败安全是否掩盖真实缺陷：``args_model.model_json_schema()`` 自身失败是否穿透。
  D. 回归：注册时机无关性、enum 真实性、既有静态工具、并发投影未被本轮改动破坏。
  E. 双路径一致性：门禁校验用的候选集与业务层用的候选集同源（不产生误拒）。
"""

import dataclasses
import logging
import threading
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from app.config import configuration
from app.core.tools.schemas.tool_definition import ToolDefinition
from app.core.tools.tool_models.child_task.delegate_task_args import DelegateTaskArgs
from app.core.tools.tool_system import ToolSystem


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
class _TinyArgs(BaseModel):
    """最小 arguments 契约，用于隔离 provider 行为。"""

    x: int = 1


def _definition(
    *,
    name: str = "probe",
    description: str = "STATIC-DESC",
    parameters_schema: dict[str, Any] | None = None,
    description_provider: Any = None,
    schema_provider: Any = None,
    args_model: type[BaseModel] = _TinyArgs,
) -> ToolDefinition:
    """构造最小 ToolDefinition，暴露描述/schema 投影路径。"""

    return ToolDefinition(
        name=name,
        description=description,
        permission="probe",
        handler=lambda **_: None,
        args_model=args_model,
        parameters_schema=parameters_schema or {},
        description_provider=description_provider,
        schema_provider=schema_provider,
    )


def _events(caplog: pytest.LogCaptureFixture) -> list[str]:
    """提取捕获到的日志事件键（record.getMessage()）。"""

    return [record.getMessage() for record in caplog.records]


@pytest.fixture
def injected_catalog(monkeypatch) -> set[str]:
    """注入真实内置 agent 注册表，返回其真实候选集（monkeypatch 自动还原）。"""

    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", None, raising=False)
    monkeypatch.setattr(configuration, "_TOOL_SYSTEM", None, raising=False)
    configuration.set_tool_system(ToolSystem.build_tool_system())
    registry = configuration.build_agent_registry()
    configuration.set_agent_registry(registry)
    return registry.child_agent_ids()


# =========================================================================== #
# A. 修复 1：provider 异常兜底契约
# =========================================================================== #
def test_project_parameters_static_priority_even_when_provider_would_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[A/契约] 静态 parameters_schema 存在时优先且**不调用** provider（连异常路径都不触发）。"""

    def boom() -> dict[str, Any]:
        raise AssertionError("provider must not be called when static schema exists")

    static = {"type": "object", "properties": {"s": {"type": "string"}}}
    definition = _definition(parameters_schema=static, schema_provider=boom)

    with caplog.at_level(logging.WARNING):
        result = definition.project_parameters()

    assert result == static
    assert "tool_schema_provider_failed" not in _events(caplog)


def test_project_description_returns_provider_value_when_present(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[A/契约] description_provider 成功时取值（与 project_parameters 的静态优先语义相反）。

    记录非对称事实：description 是「provider 优先」，parameters 是「静态优先」。
    该非对称由 delegate_task 需求决定（描述必须实时），但 docstring 措辞为
    「静态值优先，其次运行期钩子」——本用例锁定**实际行为**（provider 优先），
    避免未来被按 docstring 误改。
    """

    definition = _definition(description="STATIC", description_provider=lambda: "LIVE")
    with caplog.at_level(logging.WARNING):
        assert definition.project_description() == "LIVE"
    assert "tool_description_provider_failed" not in _events(caplog)


def test_schema_provider_fallback_is_args_model_schema_exactly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[A] schema provider 失败时降级值必须**精确等于** args_model.model_json_schema()。"""

    def boom() -> dict[str, Any]:
        raise ValueError("boom")

    definition = _definition(schema_provider=boom, args_model=_TinyArgs)
    with caplog.at_level(logging.WARNING):
        assert definition.project_parameters() == _TinyArgs.model_json_schema()
    assert "tool_schema_provider_failed" in _events(caplog)


def test_description_provider_failed_event_carries_tool_and_error_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[A] 兜底日志事件键 + tool + error_type 三要素必须齐全（否则静默无从排查）。"""

    def boom() -> str:
        raise ZeroDivisionError("nope")

    definition = _definition(name="zzz_tool", description_provider=boom)
    with caplog.at_level(logging.WARNING):
        definition.project_description()

    records = [r for r in caplog.records if r.getMessage() == "tool_description_provider_failed"]
    assert records, "未写出 tool_description_provider_failed"
    assert records[0].data["tool"] == "zzz_tool"
    assert records[0].data["error_type"] == "ZeroDivisionError"


def test_schema_provider_failed_event_carries_tool_and_error_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[A] schema 兜底日志事件键 + tool + error_type 三要素必须齐全。"""

    def boom() -> dict[str, Any]:
        raise LookupError("nope")

    definition = _definition(name="qqq_tool", schema_provider=boom)
    with caplog.at_level(logging.WARNING):
        definition.project_parameters()

    records = [r for r in caplog.records if r.getMessage() == "tool_schema_provider_failed"]
    assert records, "未写出 tool_schema_provider_failed"
    assert records[0].data["tool"] == "qqq_tool"
    assert records[0].data["error_type"] == "LookupError"


# -------- BaseException 边界 -------- #
def test_keyboard_interrupt_not_swallowed_by_description_projection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[A/边界] KeyboardInterrupt 不得被 `except Exception` 吞掉（不得静默降级）。"""

    def boom() -> str:
        raise KeyboardInterrupt("ctrl-c")

    definition = _definition(description="STATIC", description_provider=boom)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(KeyboardInterrupt):
            definition.project_description()
    assert "tool_description_provider_failed" not in _events(caplog)


def test_system_exit_not_swallowed_by_schema_projection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[A/边界] SystemExit 不得被吞掉。"""

    def boom() -> dict[str, Any]:
        raise SystemExit(3)

    definition = _definition(schema_provider=boom)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(SystemExit):
            definition.project_parameters()
    assert "tool_schema_provider_failed" not in _events(caplog)


def test_memory_error_is_contained_by_schema_projection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[A/边界] MemoryError 是 Exception 子类 → 按实现被兜底降级（锁定实际行为）。"""

    def boom() -> dict[str, Any]:
        raise MemoryError("oom")

    definition = _definition(schema_provider=boom, args_model=_TinyArgs)
    with caplog.at_level(logging.WARNING):
        result = definition.project_parameters()
    assert result == _TinyArgs.model_json_schema()
    assert "tool_schema_provider_failed" in _events(caplog)


def test_to_model_definition_composition_matches_projections(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[A/组合] to_model_tool_definition 必须等于两个 project_* 的组合投影，且键集精确。"""

    def desc() -> str:
        raise ValueError("d")

    def sch() -> dict[str, Any]:
        raise ValueError("s")

    definition = _definition(
        name="combo",
        description="STATIC",
        description_provider=desc,
        schema_provider=sch,
        args_model=_TinyArgs,
    )
    with caplog.at_level(logging.WARNING):
        payload = definition.to_model_tool_definition()

    assert set(payload.keys()) == {"name", "description", "parameters"}
    assert payload["name"] == "combo"
    assert payload["description"] == definition.project_description() == "STATIC"
    assert payload["parameters"] == definition.project_parameters() == _TinyArgs.model_json_schema()


# =========================================================================== #
# B. 修复 2：硬校验是否引入新破坏
# =========================================================================== #
def test_every_real_child_id_accepted(injected_catalog: set[str]) -> None:
    """[B] 真实候选集中的每个合法 id 都必须通过（防误拒合法请求）。"""

    assert injected_catalog, "前置条件：内置注册表应有候选子 Agent"
    for agent_id in injected_catalog:
        args = DelegateTaskArgs(child_agent_id=agent_id, agent_name="t", message="p")
        assert args.child_agent_id == agent_id


def test_invalid_id_rejected_with_full_candidate_list(injected_catalog: set[str]) -> None:
    """[B] 非法 id 必须被拒，且错误消息含**全部**合法候选供模型自纠。"""

    with pytest.raises(ValidationError) as excinfo:
        DelegateTaskArgs(child_agent_id="definitely-not-real", agent_name="t", message="p")

    message = str(excinfo.value)
    for agent_id in injected_catalog:
        assert agent_id in message, f"拒绝消息缺少候选: {agent_id}"
    assert "definitely-not-real" in message


def test_invalid_id_logs_unknown_event_with_candidates(
    injected_catalog: set[str], caplog: pytest.LogCaptureFixture
) -> None:
    """[B] 非法 id 被拒须写 delegate_task_child_agent_id_unknown 事件并含候选清单。"""

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ValidationError):
            DelegateTaskArgs(child_agent_id="ghost", agent_name="t", message="p")

    records = [
        r for r in caplog.records if r.getMessage() == "delegate_task_child_agent_id_unknown"
    ]
    assert records, "未写出 delegate_task_child_agent_id_unknown"
    assert records[0].data["child_agent_id"] == "ghost"
    assert set(records[0].data["candidates"]) == injected_catalog


def test_absent_registry_passes_any_id(monkeypatch) -> None:
    """[B] 候选集不可用（注册表未注入）时必须放行，交由业务层裁决（不得全量拒绝）。"""

    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", None, raising=False)
    args = DelegateTaskArgs(child_agent_id="whatever-xyz", agent_name="t", message="p")
    assert args.child_agent_id == "whatever-xyz"


def test_broken_registry_passes_any_id(monkeypatch) -> None:
    """[B] 注册表取数抛普通异常时同样放行（降级路径不得误拒）。"""

    class _Broken:
        def child_agent_ids(self) -> set[str]:
            raise RuntimeError("boom")

    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", _Broken(), raising=False)
    args = DelegateTaskArgs(child_agent_id="whoever", agent_name="t", message="p")
    assert args.child_agent_id == "whoever"


def test_catalog_fetch_failure_logs_warning(monkeypatch, caplog: pytest.LogCaptureFixture) -> None:
    """[B] 候选集取数失败须写 delegate_task_child_agent_catalog_unavailable 事件。"""

    class _Broken:
        def child_agent_ids(self) -> set[str]:
            raise OSError("disk")

    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", _Broken(), raising=False)
    with caplog.at_level(logging.WARNING):
        DelegateTaskArgs(child_agent_id="x", agent_name="t", message="p")

    assert "delegate_task_child_agent_catalog_unavailable" in _events(caplog)


def test_blank_and_whitespace_id_rejected_when_catalog_available(
    injected_catalog: set[str],
) -> None:
    """[B/边界] 候选集可用时空串 / 纯空白 / 含空白的 id 均必须被拒（不可绕过）。"""

    for bad in ("", "   ", "\t", " code-developer", "code-developer "):
        with pytest.raises(ValidationError):
            DelegateTaskArgs(child_agent_id=bad, agent_name="t", message="p")


def test_very_long_id_rejected_when_catalog_available(injected_catalog: set[str]) -> None:
    """[B/边界] 超长 id 必须被拒（不得因长度导致比较异常或放行）。"""

    with pytest.raises(ValidationError):
        DelegateTaskArgs(child_agent_id="a" * 1_000_000, agent_name="t", message="p")


def test_non_string_id_rejected_when_catalog_available(injected_catalog: set[str]) -> None:
    """[B/边界] 非字符串（int/None/list）必须被类型层拒绝。"""

    for bad in (1, None, ["code-developer"], {"a": 1}):
        with pytest.raises(ValidationError):
            DelegateTaskArgs(child_agent_id=bad, agent_name="t", message="p")  # type: ignore[arg-type]


def test_catalog_unavailable_still_enforces_budget(monkeypatch) -> None:
    """[B/边界] 候选集不可用时预算校验仍必须生效（硬校验降级不得连带放松预算）。"""

    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", None, raising=False)
    with pytest.raises(ValidationError):
        DelegateTaskArgs(child_agent_id="x", agent_name="  ", message="p")
    with pytest.raises(ValidationError):
        DelegateTaskArgs(child_agent_id="x", agent_name="t", message="x" * 3001)


def test_catalog_unavailable_allows_blank_id_degradation(monkeypatch) -> None:
    """[B/边界/风险] 候选集不可用时空白 id **放行**——锁定实际行为，记录降级边界。

    这是修复 2 的已知降级代价：候选集不可用 ⇒ 无法裁决 空白串 是否合法 ⇒ 放行。
    本用例把该边界显式化，避免日后被误认为「空白串总被拒」。
    """

    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", None, raising=False)
    args = DelegateTaskArgs(child_agent_id="   ", agent_name="t", message="p")
    assert args.child_agent_id == "   "


# =========================================================================== #
# C. 失败安全是否掩盖真实缺陷
# =========================================================================== #
def test_args_model_schema_failure_propagates(caplog: pytest.LogCaptureFixture) -> None:
    """[C] args_model.model_json_schema() 自身失败时必须穿透（不得被 provider 兜底顺带吞掉）。"""

    class _BrokenModel(BaseModel):
        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("broken args_model")

    # 有 provider 且 provider 也失败 → 仍必须暴露 args_model 的失败
    definition = _definition(
        args_model=_BrokenModel,
        schema_provider=lambda: (_ for _ in ()).throw(ValueError("provider also boom")),
    )
    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError, match="broken args_model"):
            definition.project_parameters()


def test_args_model_schema_failure_propagates_without_provider() -> None:
    """[C] 无 provider 时 args_model 自身失败同样穿透。"""

    class _BrokenModel(BaseModel):
        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("broken args_model 2")

    definition = _definition(args_model=_BrokenModel)
    with pytest.raises(RuntimeError, match="broken args_model 2"):
        definition.project_parameters()


def test_provider_returning_non_mapping_is_not_validated(caplog: pytest.LogCaptureFixture) -> None:
    """[C/脆弱点] provider 返回非 Mapping（如 str）时不会被兜底 —— 契约泄漏被显式化。

    ``project_parameters`` 只对 provider **抛异常**兜底，不校验返回值类型；provider 若
    违反 ``-> Mapping`` 契约返回字符串，非法值会一路流向 ``bind_tools``。此处锁定实际
    行为（返回原值），供后续评估是否需要在契约层加返回值校验。
    """

    definition = _definition(schema_provider=lambda: "not-a-mapping")
    with caplog.at_level(logging.WARNING):
        result = definition.project_parameters()
    assert result == "not-a-mapping"
    assert "tool_schema_provider_failed" not in _events(caplog)


def test_provider_returning_none_description_yields_none(caplog: pytest.LogCaptureFixture) -> None:
    """[C/脆弱点] description provider 返回 None 时不会被兜底为静态值 —— 契约泄漏被显式化。"""

    definition = _definition(description="STATIC", description_provider=lambda: None)
    with caplog.at_level(logging.WARNING):
        payload = definition.to_model_tool_definition()
    assert payload["description"] is None
    assert "tool_description_provider_failed" not in _events(caplog)


# =========================================================================== #
# D. 回归：注册时机 / enum / 静态工具 / 并发
# =========================================================================== #
@pytest.fixture
def tool_system_after_injection(monkeypatch) -> ToolSystem:
    """复刻启动顺序：先 build_tool_system，再注入 agent 注册表。"""

    monkeypatch.setattr(configuration, "_TOOL_SYSTEM", None, raising=False)
    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", None, raising=False)
    tool_system = ToolSystem.build_tool_system()
    configuration.set_tool_system(tool_system)
    with pytest.raises(RuntimeError):
        configuration.get_agent_registry()
    configuration.set_agent_registry(configuration.build_agent_registry())
    return tool_system


def test_registration_timing_does_not_freeze_catalog(
    tool_system_after_injection: ToolSystem,
) -> None:
    """[D/回归] 注册早于注入也不得固化空清单——投影必须反映注入后的真实候选集。"""

    definition = tool_system_after_injection.registry.get_tool_definition("delegate_task")
    assert definition is not None
    payload = definition.to_model_tool_definition()

    real = configuration.get_agent_registry().child_agent_ids()
    assert "Available child agents" in payload["description"]
    for agent_id in real:
        assert agent_id in payload["description"]
    assert set(payload["parameters"]["properties"]["child_agent_id"]["enum"]) == real
    assert "{ids}" not in payload["parameters"]["properties"]["child_agent_id"]["description"]


def test_enum_matches_real_catalog_exactly(tool_system_after_injection: ToolSystem) -> None:
    """[D/回归] enum 与真实候选集完全相等且不含 main_agent。"""

    definition = tool_system_after_injection.registry.get_tool_definition("delegate_task")
    assert definition is not None
    enum = set(
        definition.to_model_tool_definition()["parameters"]["properties"]["child_agent_id"]["enum"]
    )
    real = configuration.get_agent_registry().child_agent_ids()
    assert enum == real
    assert "main_agent" not in enum


def test_static_tools_have_no_providers(tool_system_after_injection: ToolSystem) -> None:
    """[D/回归] 既有静态工具不得被本轮改动引入 provider。"""

    registry = tool_system_after_injection.registry
    for name in ["read_file", "write_file", "execute_terminal", "search_content", "find_files"]:
        definition = registry.get_tool_definition(name)
        assert definition is not None, f"{name} 未注册"
        assert definition.schema_provider is None
        assert definition.description_provider is None
        assert definition.to_model_tool_definition()["parameters"]


def test_concurrent_projection_stable(tool_system_after_injection: ToolSystem) -> None:
    """[D/回归] 并发投影 delegate_task 不抛异常且结果一致（8 线程 × 50 次）。"""

    definition = tool_system_after_injection.registry.get_tool_definition("delegate_task")
    assert definition is not None

    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            for _ in range(50):
                payload = definition.to_model_tool_definition()
                with lock:
                    results.append(payload)
        except BaseException as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"并发投影抛异常: {errors!r}"
    assert all(r == results[0] for r in results)


def test_normalized_keeps_providers_for_delegate_task() -> None:
    """[D/回归] schema_provider 存在时 normalized() 不得固化空 parameters_schema。"""

    from app.core.tools.tool_handler.child_task.child_agent_create import (
        build_delegate_task_definition,
    )

    definition = build_delegate_task_definition()
    normalized = definition.normalized()
    assert normalized.parameters_schema == {}
    assert normalized.schema_provider is definition.schema_provider
    assert normalized.description_provider is definition.description_provider


def test_build_delegate_task_definition_rejects_agent_summary_kwarg() -> None:
    """[D/修复3] build_delegate_task_definition 必须已在签名上移除 agent_summary。"""

    import inspect

    from app.core.tools.tool_handler.child_task.child_agent_create import (
        build_delegate_task_definition,
    )

    assert "agent_summary" not in inspect.signature(build_delegate_task_definition).parameters
    with pytest.raises(TypeError):
        build_delegate_task_definition(agent_summary="FAKE")  # type: ignore[call-arg]


def test_static_description_is_catalog_free_fallback() -> None:
    """[D/修复3] 静态兜底描述不含清单、不泄露占位符，且核心预算约束仍在。"""

    from app.core.tools.tool_handler.child_task.child_agent_create import (
        build_delegate_task_definition,
    )

    static_description = build_delegate_task_definition().description
    assert "Available child agents" not in static_description
    assert "{ids}" not in static_description
    assert "CRITICAL BUDGET LIMIT" in static_description


# =========================================================================== #
# E. 双路径一致性：门禁 vs 业务层使用同一候选集
# =========================================================================== #
def test_broken_registry_contained_in_summary_provider(monkeypatch) -> None:
    """[E] 注册表损坏时 _runtime_child_agent_summary 必须收口为空串（投影不炸）。"""

    class _Broken:
        def child_agent_summary(self) -> str:
            raise AttributeError("boom")

        def child_agent_ids(self) -> set[str]:
            raise AttributeError("boom")

    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", _Broken(), raising=False)

    from app.core.tools.tool_handler.child_task.child_agent_create import (
        _runtime_child_agent_summary,
        build_delegate_task_definition,
    )

    assert _runtime_child_agent_summary() == ""
    description = build_delegate_task_definition().to_model_tool_definition()["description"]
    assert "Available child agents" not in description
    assert "CRITICAL BUDGET LIMIT" in description


def test_dataclass_replace_preserves_providers_and_compare_ignores_them() -> None:
    """[E/语义] replace 保留 provider；provider 不参与相等性比较（compare=False）。"""

    base = _definition(schema_provider=lambda: {"a": 1})
    other = dataclasses.replace(base, schema_provider=lambda: {"b": 2})
    assert base == other
    replaced = dataclasses.replace(base, description_provider=lambda: "x")
    assert replaced.schema_provider is base.schema_provider
    assert replaced.description_provider is not None


def test_gate_rejection_for_invalid_id_carries_candidate_list(
    tool_system_after_injection: ToolSystem,
) -> None:
    """[E/强化] 准入门禁对非法 id 的拒绝观察必须携带合法候选清单（可观测、可自纠）。

    既有改写用例只断言 ``admitted is False``（弱约束）；本用例独立强化为：拒绝观察的
    文案必须真的含真实候选 id，证明「硬校验 + 候选回灌」在门禁层的完整闭环。
    """

    from pathlib import Path

    from app.core.tools.schemas.tool_call import ToolCall
    from app.core.tools.schemas.tool_execution_context import ToolExecutionContext
    from app.core.tools.tool_execute.tool_access_gate import ToolAccessGate

    gate = ToolAccessGate(tool_system_after_injection.registry)
    ctx = ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=Path.cwd())
    call = ToolCall(
        tool_name="delegate_task",
        arguments={"child_agent_id": "general", "agent_name": "t", "message": "p"},
        call_id="c1",
    )

    outcome = gate.evaluate(call, ctx)
    assert outcome.admitted is False
    assert outcome.denial is not None

    denial_text = f"{outcome.denial.content} {outcome.denial.reason}"
    for agent_id in configuration.get_agent_registry().child_agent_ids():
        assert agent_id in denial_text, f"门禁拒绝文案缺少候选: {agent_id}"


def test_gate_admits_valid_id_and_rejects_every_invalid_variant(
    tool_system_after_injection: ToolSystem,
) -> None:
    """[E/强化] 门禁对每个合法 id 放行、对多个非法变体一律拒绝（防止误拒/漏拒）。"""

    from pathlib import Path

    from app.core.tools.schemas.tool_call import ToolCall
    from app.core.tools.schemas.tool_execution_context import ToolExecutionContext
    from app.core.tools.tool_execute.tool_access_gate import ToolAccessGate

    gate = ToolAccessGate(tool_system_after_injection.registry)
    ctx = ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=Path.cwd())

    def evaluate(child_agent_id: str):
        call = ToolCall(
            tool_name="delegate_task",
            arguments={"child_agent_id": child_agent_id, "agent_name": "t", "message": "p"},
            call_id="c1",
        )
        return gate.evaluate(call, ctx)

    for agent_id in configuration.get_agent_registry().child_agent_ids():
        assert evaluate(agent_id).admitted is True, f"合法 id 被误拒: {agent_id}"

    for bad in (
        "general",
        "explorer",
        "main_agent",
        "",
        "   ",
        "CODE-DEVELOPER",
        "code-developer ",
    ):
        assert evaluate(bad).admitted is False, f"非法 id 未被拒绝: {bad!r}"
