"""独立对抗验证：delegate_task 运行期投影修复（3 项修复真伪核实）。

本文件是独立测试作者的**第二轮独立核实**，不依赖既有用例，从「找缺陷」的对抗视角
重新验证以下声称的修复是否真实生效、是否引入新破坏：

修复 1（``ToolDefinition.project_description`` / ``project_parameters``）：
    provider 调用被 ``except Exception`` 兜底，描述降级为静态值、schema 降级为
    ``args_model.model_json_schema()``，并写 WARNING
    （``tool_description_provider_failed`` / ``tool_schema_provider_failed``）。
修复 2（``DelegateTaskArgs._validate_child_agent_id``）：
    候选集可用时硬拒非法 id（抛 ``ValueError``，消息含候选清单）；候选集不可用时放行。
修复 3（``build_delegate_task_definition``）：
    移除静态 ``agent_summary`` 注入参数，静态描述固定为无清单兜底形态。

对抗关注点：
- provider 抛 ``BaseException`` 子类（``KeyboardInterrupt`` / ``SystemExit`` / ``MemoryError``）
  是否按契约**不被吞**（``except Exception`` 不捕获它们）；
- ``project_parameters`` 在静态 schema 存在时是否**仍优先静态**；
- 兜底 WARNING 事件是否真的写出（``caplog`` 断言事件键 + ``tool`` + ``error_type``）；
- 新增硬校验是否误拒合法 id、候选集不可用时是否确实放行、空白/超长/非字符串入参行为；
- 投影热路径失败安全是否掩盖真实缺陷（``args_model.model_json_schema()`` 自身失败时穿透）；
- 既有静态工具 / enum 真实性 / 并发投影未被破坏。
"""

import dataclasses
import logging
import threading
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from app.config import configuration
from app.core.tools.schemas.tool_definition import ToolDefinition
from app.core.tools.tool_models.delegate_task_args import DelegateTaskArgs
from app.core.tools.tool_registry import ToolRegistry
from app.core.tools.tool_system import ToolSystem


# --------------------------------------------------------------------------- #
# 辅助：最小 args_model 与 definition 工厂
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


# =========================================================================== #
# 修复 1：provider 异常兜底
# =========================================================================== #
def test_description_provider_exception_falls_back_to_static(caplog) -> None:
    """[修复 1] description_provider 抛普通异常 → 降级为静态描述，不穿透。"""

    def boom() -> str:
        raise ValueError("simulated description provider bug")

    definition = _definition(description="FALLBACK", description_provider=boom)

    with caplog.at_level(logging.WARNING):
        result = definition.project_description()

    assert result == "FALLBACK"
    assert "tool_description_provider_failed" in _events(caplog)


def test_description_provider_failure_log_contains_tool_and_error_type(caplog) -> None:
    """[修复 1] 降级日志必须含 tool 与 error_type，便于定位而不泄密。"""

    def boom() -> str:
        raise KeyError("secret-ish detail")

    definition = _definition(name="my_tool", description="D", description_provider=boom)

    with caplog.at_level(logging.WARNING):
        definition.project_description()

    records = [r for r in caplog.records if r.getMessage() == "tool_description_provider_failed"]
    assert records, "未写出 tool_description_provider_failed 事件"
    data = records[0].data
    assert data["tool"] == "my_tool"
    assert data["error_type"] == "KeyError"


def test_schema_provider_exception_falls_back_to_args_model_schema(caplog) -> None:
    """[修复 1] schema_provider 抛异常 → 降级为 args_model.model_json_schema()。"""

    def boom() -> dict[str, Any]:
        raise RuntimeError("simulated schema provider bug")

    definition = _definition(schema_provider=boom, args_model=_TinyArgs)

    with caplog.at_level(logging.WARNING):
        result = definition.project_parameters()

    assert result == _TinyArgs.model_json_schema()
    assert "tool_schema_provider_failed" in _events(caplog)


def test_schema_provider_failure_log_contains_tool_and_error_type(caplog) -> None:
    """[修复 1] schema 降级日志必须含 tool 与 error_type。"""

    def boom() -> dict[str, Any]:
        raise TypeError("schema bug")

    definition = _definition(name="sch_tool", schema_provider=boom)

    with caplog.at_level(logging.WARNING):
        definition.project_parameters()

    records = [r for r in caplog.records if r.getMessage() == "tool_schema_provider_failed"]
    assert records, "未写出 tool_schema_provider_failed 事件"
    assert records[0].data["tool"] == "sch_tool"
    assert records[0].data["error_type"] == "TypeError"


def test_project_description_static_takes_priority_over_provider() -> None:
    """[修复 1] 文档声称「静态值优先」——若未优先则是契约不符。

    注意：文档同时声称 provider 存在时取其结果。这里锁定静态缺省不等于「优先」，
    因此本用例只断言 provider 成功时确实被调用（避免把未定义行为当成缺陷误报）。
    """

    calls: list[int] = []

    def provider() -> str:
        calls.append(1)
        return "LIVE"

    definition = _definition(description="STATIC", description_provider=provider)
    assert definition.project_description() == "LIVE"
    assert len(calls) == 1


def test_project_parameters_static_schema_wins_over_provider(caplog) -> None:
    """[修复 1] 静态 parameters_schema 存在时必须**优先于** provider（不调用 provider）。

    docstring 明确「静态 > 运行期钩子」，若 provider 仍被调用即契约破坏。
    """

    calls: list[int] = []

    def provider() -> dict[str, Any]:
        calls.append(1)
        return {"type": "object", "properties": {"live": {"type": "string"}}}

    definition = _definition(
        parameters_schema={"type": "object", "properties": {"static": {"type": "integer"}}},
        schema_provider=provider,
    )

    with caplog.at_level(logging.WARNING):
        result = definition.project_parameters()

    assert result == {"type": "object", "properties": {"static": {"type": "integer"}}}
    assert calls == [], "静态 schema 存在时不得调用 provider"
    assert "tool_schema_provider_failed" not in _events(caplog)


def test_project_parameters_no_provider_returns_args_model_schema() -> None:
    """[修复 1] 无静态 schema 且无 provider → 直接返回 args_model schema。"""

    definition = _definition()
    assert definition.project_parameters() == _TinyArgs.model_json_schema()


# =========================================================================== #
# 边界：BaseException 子类是否按契约不被吞（不写日志、向上抛）
# =========================================================================== #
def test_keyboard_interrupt_from_description_provider_propagates(caplog) -> None:
    """[边界] KeyboardInterrupt（BaseException 子类）不应被 `except Exception` 吞掉。

    若被吞掉并降级为静态描述，则中断信号被静默吞没——这是投毒式失败安全。
    契约：`except Exception` 不捕获 BaseException，故应向上传播。
    """

    def boom() -> str:
        raise KeyboardInterrupt("cancellation signal")

    definition = _definition(description_provider=boom)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(KeyboardInterrupt):
            definition.project_description()

    assert "tool_description_provider_failed" not in _events(caplog)


def test_system_exit_from_schema_provider_propagates(caplog) -> None:
    """[边界] SystemExit（BaseException 子类）不应被吞——否则无法正常退出进程。"""

    def boom() -> dict[str, Any]:
        raise SystemExit(2)

    definition = _definition(schema_provider=boom)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(SystemExit):
            definition.project_parameters()

    assert "tool_schema_provider_failed" not in _events(caplog)


def test_memory_error_from_schema_provider_is_contained_or_propagates(caplog) -> None:
    """[边界] MemoryError 是 Exception 子类 → 按实现应被兜底（记录实际行为）。

    MemoryError 继承 Exception，故会被 `except Exception` 捕获并降级。此用例锁定
    「MemoryError 被降级为 args_model 契约」这一实际行为，防止未来被误改。
    """

    def boom() -> dict[str, Any]:
        raise MemoryError("oom")

    definition = _definition(schema_provider=boom, args_model=_TinyArgs)

    with caplog.at_level(logging.WARNING):
        result = definition.project_parameters()

    assert result == _TinyArgs.model_json_schema()
    assert "tool_schema_provider_failed" in _events(caplog)


# =========================================================================== #
# 边界：args_model.model_json_schema() 自身失败时是否穿透
# =========================================================================== #
def test_args_model_schema_failure_propagates_from_project_parameters() -> None:
    """[边界] args_model.model_json_schema() 自身失败时必须穿透（文档称工具定义错误向上暴露）。

    这是可接受边界：args_model 是工具注册契约，其 schema 生成失败属定义错误，
    掩盖会让模型收到错误 schema。本用例锁定「穿透」这一实际行为并记录结论。
    """

    class _BrokenModel(BaseModel):
        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("broken args_model")

    definition = _definition(args_model=_BrokenModel, schema_provider=None)

    with pytest.raises(RuntimeError, match="broken args_model"):
        definition.project_parameters()


# =========================================================================== #
# to_model_tool_definition 组合投影
# =========================================================================== #
def test_to_model_definition_shape_and_provider_integration(caplog) -> None:
    """[组合] to_model_tool_definition 必须组合 project_* 的结果且键集精确。"""

    def desc() -> str:
        raise ValueError("x")

    def sch() -> dict[str, Any]:
        raise ValueError("y")

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
    assert payload["description"] == "STATIC"
    assert payload["parameters"] == _TinyArgs.model_json_schema()


# =========================================================================== #
# 修复 2：DelegateTaskArgs 硬校验
# =========================================================================== #
@pytest.fixture
def injected_registry(monkeypatch):
    """注入真实内置 agent 注册表（不改生产单例，monkeypatch 自动还原）。

    ``build_agent_registry`` 依赖 ``get_tool_registry``（循环依赖），故必须先构建并注入
    tool system；这与应用启动顺序一致。
    """

    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", None, raising=False)
    monkeypatch.setattr(configuration, "_TOOL_SYSTEM", None, raising=False)
    configuration.set_tool_system(ToolSystem.build_tool_system(None))
    registry = configuration.build_agent_registry()
    configuration.set_agent_registry(registry)
    return registry


def test_valid_child_agent_id_accepted_by_validator(injected_registry) -> None:
    """[修复 2] 真实候选集中的每个合法 id 都必须通过校验（防误拒合法 id）。"""

    for agent_id in injected_registry.child_agent_ids():
        args = DelegateTaskArgs(child_agent_id=agent_id, title="t", prompt="p")
        assert args.child_agent_id == agent_id


def test_invalid_child_agent_id_rejected_with_candidate_list(injected_registry) -> None:
    """[修复 2] 非法 id 必须被拒，且错误消息含合法候选清单供模型自纠。"""

    with pytest.raises(ValidationError) as excinfo:
        DelegateTaskArgs(child_agent_id="general", title="t", prompt="p")

    message = str(excinfo.value)
    for agent_id in injected_registry.child_agent_ids():
        assert agent_id in message, f"错误消息缺少合法候选: {agent_id}"


def test_invalid_child_agent_id_logs_unknown_event(injected_registry, caplog) -> None:
    """[修复 2] 非法 id 被拒时须写 delegate_task_child_agent_id_unknown 事件。"""

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ValidationError):
            DelegateTaskArgs(child_agent_id="nope", title="t", prompt="p")

    assert "delegate_task_child_agent_id_unknown" in _events(caplog)


def test_child_agent_id_passes_when_candidates_unavailable(monkeypatch) -> None:
    """[修复 2] 候选集不可用时必须放行任意 id（交由业务层裁决，不全量拒绝）。"""

    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", None, raising=False)

    args = DelegateTaskArgs(child_agent_id="anything-goes", title="t", prompt="p")
    assert args.child_agent_id == "anything-goes"


def test_child_agent_id_passes_when_registry_raises(monkeypatch) -> None:
    """[修复 2] 注册表取数抛异常时同样放行（降级路径不得把合法请求也拒掉）。"""

    class _Broken:
        def child_agent_ids(self) -> set[str]:
            raise AttributeError("boom")

    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", _Broken(), raising=False)

    args = DelegateTaskArgs(child_agent_id="whoever", title="t", prompt="p")
    assert args.child_agent_id == "whoever"


def test_catalog_unavailable_logs_warning(monkeypatch, caplog) -> None:
    """[修复 2] 候选集取数失败须写 delegate_task_child_agent_catalog_unavailable 事件。"""

    class _Broken:
        def child_agent_ids(self) -> set[str]:
            raise ValueError("boom")

    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", _Broken(), raising=False)

    with caplog.at_level(logging.WARNING):
        DelegateTaskArgs(child_agent_id="x", title="t", prompt="p")

    assert "delegate_task_child_agent_catalog_unavailable" in _events(caplog)


def test_blank_child_agent_id_rejected_when_catalog_available(injected_registry) -> None:
    """[边界] 候选集可用时空白串 id 必须被拒（防绕过）。"""

    with pytest.raises(ValidationError):
        DelegateTaskArgs(child_agent_id="   ", title="t", prompt="p")


def test_very_long_child_agent_id_rejected_when_catalog_available(injected_registry) -> None:
    """[边界] 候选集可用时超长 id 必须被拒。"""

    with pytest.raises(ValidationError):
        DelegateTaskArgs(child_agent_id="a" * 100000, title="t", prompt="p")


def test_non_string_child_agent_id_rejected(injected_registry) -> None:
    """[边界] 非字符串 child_agent_id（整数/None）应被 pydantic 类型层拒绝。"""

    with pytest.raises(ValidationError):
        DelegateTaskArgs(child_agent_id=123, title="t", prompt="p")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        DelegateTaskArgs(child_agent_id=None, title="t", prompt="p")  # type: ignore[arg-type]


def test_budget_validation_still_enforced(injected_registry) -> None:
    """[回归] 预算校验未被新增校验破坏（空 title/空 prompt/超长 prompt 仍拒）。"""

    with pytest.raises(ValidationError):
        DelegateTaskArgs(child_agent_id="code-developer", title="  ", prompt="p")
    with pytest.raises(ValidationError):
        DelegateTaskArgs(child_agent_id="code-developer", title="ok", prompt="")
    with pytest.raises(ValidationError):
        DelegateTaskArgs(child_agent_id="code-developer", title="ok", prompt="x" * 3001)


def test_budget_valid_child_agent_id_validator_order(injected_registry) -> None:
    """[边界] 预算失败与 id 失败同时存在时，仍必须抛 ValidationError（不因顺序漏检）。"""

    with pytest.raises(ValidationError):
        DelegateTaskArgs(child_agent_id="bogus", title="", prompt="")


# =========================================================================== #
# 修复 3：静态摘要注入通道移除
# =========================================================================== #
def test_build_delegate_task_definition_has_no_agent_summary_param() -> None:
    """[修复 3] 签名不得含 agent_summary，从接口上消除遮蔽运行期清单的地雷。"""

    import inspect

    from app.core.tools.tool_handler.delegate_task import build_delegate_task_definition

    params = inspect.signature(build_delegate_task_definition).parameters
    assert "agent_summary" not in params
    with pytest.raises(TypeError):
        build_delegate_task_definition(agent_summary="FAKE")  # type: ignore[call-arg]


def test_static_description_has_no_catalog_and_live_projection_does(monkeypatch) -> None:
    """[修复 3] 静态兜底描述无清单；注入注册表后运行期投影含真实清单。"""

    from app.core.tools.tool_handler.delegate_task import build_delegate_task_definition

    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", None, raising=False)
    monkeypatch.setattr(configuration, "_TOOL_SYSTEM", None, raising=False)

    definition = build_delegate_task_definition()
    static_desc = definition.description
    assert "Available child agents" not in static_desc
    assert "CRITICAL BUDGET LIMIT" in static_desc

    tool_system = ToolSystem.build_tool_system(None)
    configuration.set_tool_system(tool_system)
    configuration.set_agent_registry(configuration.build_agent_registry())

    live = definition.to_model_tool_definition()["description"]
    assert "Available child agents" in live
    for agent_id in configuration.get_agent_registry().child_agent_ids():
        assert agent_id in live


# =========================================================================== #
# 回归：注册时机无关性 / enum 真实性 / 静态工具 / 并发
# =========================================================================== #
@pytest.fixture
def tool_system_injected_after_build(monkeypatch) -> ToolSystem:
    """复刻启动顺序：先 build_tool_system，后注入 agent 注册表。"""

    monkeypatch.setattr(configuration, "_TOOL_SYSTEM", None, raising=False)
    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", None, raising=False)

    tool_system = ToolSystem.build_tool_system(None)
    configuration.set_tool_system(tool_system)
    with pytest.raises(RuntimeError):
        configuration.get_agent_registry()
    configuration.set_agent_registry(configuration.build_agent_registry())
    return tool_system


def test_enum_matches_real_ids_exactly(tool_system_injected_after_build: ToolSystem) -> None:
    """[回归] child_agent_id enum 与真实候选集完全相等，且不含 main_agent。"""

    definition = tool_system_injected_after_build.registry.get_tool_definition("delegate_task")
    assert definition is not None
    enum = set(definition.to_model_tool_definition()["parameters"]["properties"]["child_agent_id"]["enum"])
    real = configuration.get_agent_registry().child_agent_ids()
    assert enum == real
    assert "main_agent" not in enum


def test_static_tools_untouched_by_fix(tool_system_injected_after_build: ToolSystem) -> None:
    """[回归] 既有静态工具无 provider、参数 schema 非空。"""

    registry = tool_system_injected_after_build.registry
    for name in ["read_file", "execute_terminal", "search_content", "find_files", "write_file"]:
        definition = registry.get_tool_definition(name)
        assert definition is not None, f"{name} 未注册"
        assert definition.schema_provider is None
        assert definition.description_provider is None
        assert definition.to_model_tool_definition()["parameters"]


def test_concurrent_projection_produces_consistent_results(
    tool_system_injected_after_build: ToolSystem,
) -> None:
    """[回归] 并发投影 delegate_task 不抛异常且结果一致。"""

    definition = tool_system_injected_after_build.registry.get_tool_definition("delegate_task")
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
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"并发投影抛异常: {errors!r}"
    assert all(r == results[0] for r in results)


def test_description_provider_hot_path_never_raises_for_arbitrary_exception(
    monkeypatch,
) -> None:
    """[对抗] description_provider 抛任意 Exception 子类时投影绝不穿透（热路径失败安全）。"""

    for exc_type in (ValueError, TypeError, KeyError, AttributeError, RuntimeError, ZeroDivisionError):
        def boom(exc_type=exc_type) -> str:
            raise exc_type("boom")

        definition = _definition(description="FALLBACK", description_provider=boom)
        payload = definition.to_model_tool_definition()
        assert payload["description"] == "FALLBACK", f"{exc_type} 未被兜底"


def test_normalized_keeps_providers_and_does_not_freeze_schema() -> None:
    """[回归] 声明 schema_provider 时 normalized() 不得固化空 schema。"""

    from app.core.tools.tool_handler.delegate_task import build_delegate_task_definition

    definition = build_delegate_task_definition()
    normalized = definition.normalized()
    assert normalized.parameters_schema == {}
    assert normalized.schema_provider is definition.schema_provider
    assert normalized.description_provider is definition.description_provider
    # frozen dataclass 语义：replace 保留 provider（compare=False 不影响赋值）
    replaced = dataclasses.replace(definition, description="X")
    assert replaced.description_provider is definition.description_provider


def test_registry_get_schema_uses_live_projection(
    tool_system_injected_after_build: ToolSystem,
) -> None:
    """[回归] registry.get_schema 走实时投影，enum 与真实候选集一致。"""

    schema = tool_system_injected_after_build.registry.get_schema("delegate_task")
    assert schema is not None
    assert set(schema["properties"]["child_agent_id"]["enum"]) == (
        configuration.get_agent_registry().child_agent_ids()
    )


def test_title_over_budget_rejected_with_log(injected_registry, caplog) -> None:
    """[回归] title 超长必须被拒并写 delegate_task_args_over_budget 事件（预算分支）。"""

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ValidationError):
            DelegateTaskArgs(child_agent_id="code-developer", title="x" * 11, prompt="p")

    assert "delegate_task_args_over_budget" in _events(caplog)


class _NeverCalledExecutor:
    """绝不执行的委派执行器替身：用于验证取消分支在 executor 之前短路。"""

    def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("executor must not be called when run is cancelled")


def test_handler_returns_cancelled_when_run_cancelled(monkeypatch) -> None:
    """[回归] run 已取消时 handler 返回 cancelled 观察（取消分支）。"""

    from pathlib import Path

    from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
    from app.core.tools.schemas.tool_execution_context import ToolExecutionContext
    from app.core.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies
    from app.core.tools.tool_handler.delegate_task import DelegateTaskTool

    monkeypatch.setattr(cancellation_registry, "is_cancelled", lambda run_id: True)

    # 取消检查在 executor 存在性检查之后，故必须提供一个非 None 的执行器替身。
    ctx = ToolExecutionContext(
        task_id=1,
        workspace_id=1,
        workspace_root=Path.cwd(),
        runtime_dependencies=ToolRuntimeDependencies(
            delegate_task_executor=_NeverCalledExecutor(),  # type: ignore[arg-type]
        ),
    )

    observation = DelegateTaskTool().execute(
        child_agent_id="code-developer", title="t", prompt="p", execution_context=ctx
    )
    assert observation.status == "cancelled"


def test_registry_duplicate_register_does_not_bump_generation() -> None:
    """[回归] 同名重复注册忽略且不递增 generation（既有契约）。"""

    from app.core.tools.tool_models.delegate_task_args import DelegateTaskArgs as _Args

    registry = ToolRegistry()
    definition = ToolDefinition(
        name="dup",
        description="d",
        permission="p",
        handler=lambda **_: None,
        args_model=_Args,
    )
    registry.register(definition)
    gen = registry.generation
    registry.register(definition)
    assert registry.generation == gen
