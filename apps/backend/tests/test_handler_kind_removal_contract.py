"""``ToolDefinition.handler_kind`` / ``parallel_group`` 及相关调度通道删除后的缺陷发现型验证。

本次改动删除了：
- ``ToolDefinition.handler_kind`` 字段与 ``AsyncToolHandler`` Protocol；
- ``ToolExecutor.execute_async`` / ``_execute_inner_async`` / ``_run_async_handler`` /
  ``_wait_for_cancellation`` / ``_await_cancelled_task``，以及 ``execute`` 内
  ``handler_kind != "sync"`` 断言；
- ``WorkflowOperations._handler_kind_by_name`` / ``_execute_async_tool_call``（串行调用统一走
  ``asyncio.to_thread(self._execute_tool_call, ...)``），``_is_parallel_call`` 改为只看
  ``parallel_mode``；
- ``ToolRegistry._validate_handler_contract``（注册期不再校验 handler 与 coroutine 一致性）；
- ``ToolDefinition.parallel_group``（零消费者声明式字段）与 ``delegate_task_group`` 分组声明；
- ``ToolHandlerRunner`` 的 public 包装层 ``normalize_result`` / ``build_cancellation_check`` /
  ``cleanup_cancellation_signal``，调用点下沉到私有 ``_build_cancel_check`` /
  ``_clear_tool_call_cancellation``。

本模块从「删除后仍须成立的不变量」出发做对抗性验证：残留符号、投影保真、注册语义、
调度分支（串行/并行/未知工具）与状态副作用。测试只断言现状，不修改生产代码。
"""

from __future__ import annotations

import asyncio
import dataclasses
import gc
import threading
import time
import warnings
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import BaseModel

from app.core.observability.tool_trace_recorder import _NullToolTraceRecorder
from app.core.tools.schemas import (
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.schemas import tool_definition as tool_definition_module
from app.core.tools.tool_execute.tool_executor import ToolExecutor
from app.core.tools.tool_registry import ToolRegistry
from app.core.workflows import workflow_operations as workflow_operations_module
from app.core.workflows.workflow_operations import WorkflowOperations

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# 夹具 / 工具函数
# --------------------------------------------------------------------------- #


class _ProbeArgs(BaseModel):
    """最小参数模型：所有工具定义共用，避免 pydantic 校验成为噪声源。"""

    message: str = ""


def _sync_probe_handler(**kwargs: Any) -> ToolObservation:
    """模块级共享 handler：同一函数对象参与 dataclass 相等性，避免闭包身份噪声。"""

    del kwargs
    return ToolObservation(tool_name="probe_tool", status="success", content="probe:ok")


def _make_definition(name: str = "probe_tool", **overrides: Any) -> ToolDefinition:
    """构造一个最小可注册的 ``ToolDefinition``（默认 handler 返回固定成功观察）。"""

    base: dict[str, Any] = {
        "name": name,
        "description": "probe",
        "permission": "safe_read",
        "handler": _sync_probe_handler,
        "args_model": _ProbeArgs,
    }
    base.update(overrides)
    return ToolDefinition(**base)


class _SleepingExecutor:
    """按 ``call_id`` 睡眠的假执行器，记录调用顺序、worker 线程与并发峰值。

    用于精确断言调度层行为（顺序 / 是否离开事件循环线程 / 是否真并发），
    不触碰真实 handler 与子进程。
    """

    def __init__(self, delays: Mapping[str, float] | None = None) -> None:
        self._delays = dict(delays or {})
        self._lock = threading.Lock()
        self.call_order: list[str] = []
        self.thread_idents: list[int] = []
        self.active = 0
        self.max_active = 0

    def execute(
        self,
        call: ToolCall,
        execution_context: object = None,
        allowed_tool_names: object = None,
    ) -> ToolObservation:
        del execution_context, allowed_tool_names
        with self._lock:
            self.call_order.append(call.call_id)
            self.thread_idents.append(threading.get_ident())
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            delay = self._delays.get(call.call_id, 0.0)
            if delay:
                time.sleep(delay)
        finally:
            with self._lock:
                self.active -= 1
        return ToolObservation(
            tool_name=call.tool_name,
            status="success",
            content=call.call_id,
            tool_call_id=call.call_id,
        )


def _operations_with_executor(
    executor: object,
    *,
    parallel_modes: Mapping[str, str] | None = None,
    allowed: frozenset[str] | None = None,
    execution_context: ToolExecutionContext | None = None,
) -> WorkflowOperations:
    """绕过 ``__init__``（不需要模型/DB 依赖）装配只含调度所需字段的门面。"""

    operations = WorkflowOperations.__new__(WorkflowOperations)
    operations._executor = cast(ToolExecutor, executor)
    operations._trace_recorder = _NullToolTraceRecorder()
    operations._execution_context = execution_context
    operations._allowed_tool_names = frozenset(allowed or ())
    operations._parallel_mode_by_name = dict(parallel_modes or {})
    return operations


def _call(name: str, call_id: str, arguments: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(tool_name=name, arguments=arguments or {}, call_id=call_id)


# --------------------------------------------------------------------------- #
# 分组 1：ToolDefinition 删除字段后的构造 / frozen / normalized 投影
# --------------------------------------------------------------------------- #


def test_tool_definition_constructs_without_handler_kind_field() -> None:
    """删除字段后定义仍可构造，且 handler_kind 不再是数据类字段（防残留字段复活）。"""

    definition = _make_definition()

    assert definition.name == "probe_tool"
    assert not hasattr(definition, "handler_kind")
    field_names = {field.name for field in dataclasses.fields(ToolDefinition)}
    assert "handler_kind" not in field_names


def test_async_tool_handler_protocol_symbol_is_gone() -> None:
    """AsyncToolHandler Protocol 已删除：模块与包导出都不应再暴露该符号（防半删残留）。"""

    assert not hasattr(tool_definition_module, "AsyncToolHandler")


def test_tool_definition_is_still_frozen() -> None:
    """frozen 语义不因字段删除而改变：任何字段赋值都必须抛 FrozenInstanceError。"""

    definition = _make_definition()

    with pytest.raises(FrozenInstanceError):
        definition.name = "renamed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        definition.parallel_mode = "parallel"  # type: ignore[misc]


def test_tool_definition_default_contract_values_after_field_removal() -> None:
    """字段删除后默认值必须保持原契约（防默认值被顺手改动造成调度/隔离行为漂移）。"""

    definition = _make_definition()

    assert definition.parameters_schema == {}
    assert definition.timeout_seconds == 10.0
    assert definition.risk_level == "low"
    assert definition.resource_keys == ()
    assert definition.display is None
    assert definition.execution_mode == "thread"
    assert definition.parallel_mode == "serial"
    assert definition.description_provider is None
    assert definition.schema_provider is None


def test_tool_definition_requires_handler_and_args_model() -> None:
    """必填契约字段缺失时必须抛 TypeError（防止删除字段时顺手放宽构造约束）。"""

    with pytest.raises(TypeError):
        ToolDefinition(  # type: ignore[call-arg]
            name="missing_handler",
            description="d",
            permission="safe_read",
            args_model=_ProbeArgs,
        )
    with pytest.raises(TypeError):
        ToolDefinition(  # type: ignore[call-arg]
            name="missing_args_model",
            description="d",
            permission="safe_read",
            handler=_sync_probe_handler,
        )


def test_model_projection_exposes_only_name_description_parameters() -> None:
    """模型可见投影的键必须恰为 name/description/parameters：被删字段不得泄漏进下发结构。"""

    definition = _make_definition(parameters_schema={"type": "object", "properties": {}})

    projection = definition.to_model_tool_definition()

    assert set(projection) == {"name", "description", "parameters"}
    assert projection["name"] == "probe_tool"


def test_normalized_derives_schema_when_no_parameters_schema() -> None:
    """无 parameters_schema 且无 schema_provider 时，normalized() 用 args_model 派生 schema。"""

    definition = _make_definition()
    assert definition.parameters_schema == {}

    normalized = definition.normalized()

    assert normalized is not definition
    assert normalized.parameters_schema != {}
    assert normalized.parameters_schema == _ProbeArgs.model_json_schema()
    assert normalized.name == definition.name


def test_normalized_returns_self_when_parameters_schema_supplied() -> None:
    """已显式提供 parameters_schema 时 normalized() 必须原样返回自身（不固化、不复制）。"""

    schema = {"type": "object", "properties": {"message": {"type": "string"}}}
    definition = _make_definition(parameters_schema=schema)

    assert definition.normalized() is definition


def test_normalized_returns_self_when_schema_provider_supplied() -> None:
    """声明 schema_provider 的工具不固化 schema：normalized() 返回自身，留给每次投影实时生成。"""

    calls: list[int] = []

    def provider() -> Mapping[str, Any]:
        calls.append(1)
        return {"type": "object", "properties": {"live": {"type": "string"}}}

    definition = _make_definition(schema_provider=provider)

    normalized = definition.normalized()

    assert normalized is definition
    assert calls == [], "normalized() 不应触发 schema_provider（否则等于启动期固化）"


def test_normalized_preserves_every_carried_contract_field() -> None:
    """normalized() 生成的副本必须逐字段保留调度/展示/钩子契约（防新副本漏字段）。"""

    def description_provider() -> str:
        return "live description"

    definition = _make_definition(
        timeout_seconds=3.5,
        risk_level="high",
        resource_keys=("filesystem", "shell"),
        execution_mode="process",
        parallel_mode="parallel",
        description_provider=description_provider,
    )

    normalized = definition.normalized()

    assert normalized.timeout_seconds == 3.5
    assert normalized.risk_level == "high"
    assert normalized.resource_keys == ("filesystem", "shell")
    assert normalized.execution_mode == "process"
    assert normalized.parallel_mode == "parallel"
    assert normalized.description_provider is description_provider
    assert normalized.schema_provider is None
    assert normalized.args_model is _ProbeArgs
    assert normalized.permission == "safe_read"
    assert normalized.handler is definition.handler
    assert normalized.display is definition.display
    # 未声明 schema_provider 时参数 schema 被派生，故二者不相等；派生后应幂等。
    assert normalized.normalized() == normalized


def test_normalized_evaluates_description_provider_once_via_model_projection() -> None:
    """记录既有行为（非本次改动引入）：normalized() 经模型投影会真实调用 description_provider。

    潜在缺陷类型：注释声称「运行期钩子不在注册期固化」，但注册期 normalize 会**求值**该钩子
    （虽然不落库）。若钩子依赖启动期尚不可用的运行期单例，会在此产生一次无意义调用/告警。
    """

    calls: list[int] = []

    def description_provider() -> str:
        calls.append(1)
        return "live description"

    definition = _make_definition(description_provider=description_provider)

    normalized = definition.normalized()

    assert calls == [1], "normalized() 的模型投影会调用一次 description_provider"
    assert normalized.description == "probe", "派生副本不得固化钩子结果，仍保留静态兜底描述"
    assert normalized.description_provider is description_provider


def test_normalized_keeps_parallel_mode_so_parallel_scheduling_is_not_lost() -> None:
    """parallel_mode 经 normalized() 后必须仍是 "parallel"（否则并行分组被静默降级为串行）。"""

    definition = _make_definition(name="parallel_tool", parallel_mode="parallel")

    normalized = definition.normalized()

    assert normalized.parallel_mode == "parallel"
    assert (
        normalized == _make_definition(name="parallel_tool", parallel_mode="parallel").normalized()
    )


def test_description_provider_is_excluded_from_equality_and_hash() -> None:
    """compare=False 契约：description_provider 是行为而非身份，不参与相等性与哈希。"""

    def provider_a() -> str:
        return "a"

    def provider_b() -> str:
        return "b"

    left = _make_definition(description_provider=provider_a)
    right = _make_definition(description_provider=provider_b)

    assert left == right
    # 结构性佐证：该字段在数据类声明里 compare=False（否则相等性断言即为假阳性）。
    compare_flag = {f.name: f.compare for f in dataclasses.fields(ToolDefinition)}
    assert compare_flag["description_provider"] is False
    # 对照：参与比较的静态字段不同则必须不等（证明上面的相等断言不是恒真）。
    assert left != _make_definition(description="different", description_provider=provider_a)
    # 但行为投影确实不同（compare=False 并不意味着投影被忽略）。
    assert left.project_description() == "a"
    assert right.project_description() == "b"


def test_schema_provider_is_excluded_from_equality_and_hash() -> None:
    """compare=False 契约：schema_provider 同样不参与相等性与哈希。"""

    def provider_a() -> Mapping[str, Any]:
        return {"type": "object", "properties": {"a": {"type": "string"}}}

    def provider_b() -> Mapping[str, Any]:
        return {"type": "object", "properties": {"b": {"type": "integer"}}}

    left = _make_definition(schema_provider=provider_a)
    right = _make_definition(schema_provider=provider_b)

    assert left == right
    compare_flag = {f.name: f.compare for f in dataclasses.fields(ToolDefinition)}
    assert compare_flag["schema_provider"] is False
    assert left != _make_definition(description="different", schema_provider=provider_a)
    assert left.project_parameters() == provider_a()
    assert right.project_parameters() == provider_b()


def test_project_parameters_falls_back_to_args_model_when_provider_raises() -> None:
    """schema_provider 抛异常必须降级到 args_model 契约（热路径异常不得穿透）。"""

    def broken() -> Mapping[str, Any]:
        raise RuntimeError("provider exploded")

    definition = _make_definition(schema_provider=broken)

    assert definition.project_parameters() == _ProbeArgs.model_json_schema()


def test_project_description_degrades_to_static_when_provider_raises() -> None:
    """description_provider 抛异常必须降级为静态描述且不外抛（下发模型热路径不得炸穿 run）。"""

    def broken() -> str:
        raise ValueError("provider exploded")

    definition = _make_definition(description_provider=broken)

    assert definition.project_description() == "probe"
    assert definition.to_model_tool_definition()["description"] == "probe"


# --------------------------------------------------------------------------- #
# 分组 2：ToolRegistry.register 既有行为不回归
# --------------------------------------------------------------------------- #


def test_register_stores_normalized_definition_and_bumps_generation() -> None:
    """注册落库的是 normalized() 后的定义，且 generation 自增 1。"""

    registry = ToolRegistry()
    assert registry.generation == 0

    registry.register(_make_definition())

    stored = registry.get_tool_definition("probe_tool")
    assert stored is not None
    assert stored.parameters_schema == _ProbeArgs.model_json_schema()
    assert registry.generation == 1


def test_register_duplicate_name_is_skipped_and_generation_unchanged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """重复同名注册被跳过：generation 不变、先注册的定义保持有效、写告警日志。"""

    registry = ToolRegistry()
    first = _make_definition(description="first")
    second = _make_definition(description="second")

    with caplog.at_level("WARNING"):
        registry.register(first)
        registry.register(second)

    stored = registry.get_tool_definition("probe_tool")
    assert stored is not None
    assert stored.description == "first"
    assert registry.generation == 1
    assert any(record.message == "tool_already_registered" for record in caplog.records)


def test_register_none_is_silent_noop() -> None:
    """register(None) 返回而不抛异常，且不改变 generation。"""

    registry = ToolRegistry()

    assert registry.register(None) is None  # type: ignore[arg-type]
    assert registry.generation == 0
    assert registry.get_all_tool_names() == []


def test_register_non_tool_definition_raises_type_error() -> None:
    """非 ToolDefinition 入参必须抛 TypeError（错误消息含实际类型名）。"""

    registry = ToolRegistry()

    with pytest.raises(TypeError) as excinfo:
        registry.register("not-a-definition")  # type: ignore[arg-type]

    assert "expected ToolDefinition" in str(excinfo.value)
    assert "str" in str(excinfo.value)
    assert registry.generation == 0


def test_registry_no_longer_validates_handler_contract() -> None:
    """``_validate_handler_contract`` 已删除：注册期不再存在 handler/协程一致性校验入口。"""

    assert not hasattr(ToolRegistry, "_validate_handler_contract")


def test_register_accepts_coroutine_handler_without_raising() -> None:
    """删除注册期校验后，async handler 不再被拒（记录行为变更：注册期不再拦截异步 handler）。

    潜在缺陷类型：丢失「async handler 必须 parallel_mode=serial」与
    「声明与实现一致」两类守卫，异步 handler 会被静默放行。
    """

    async def async_handler(**kwargs: Any) -> ToolObservation:
        del kwargs
        return ToolObservation(tool_name="async_tool", status="success", content="ok")

    registry = ToolRegistry()
    definition = _make_definition(
        name="async_tool", handler=async_handler, parallel_mode="parallel"
    )

    registry.register(definition)  # 删除前此处会抛 ValueError/TypeError

    stored = registry.get_tool_definition("async_tool")
    assert stored is not None
    assert stored.parallel_mode == "parallel"
    assert registry.generation == 1


def test_concurrent_registration_of_same_name_increments_generation_once() -> None:
    """并发同名注册的副作用必须收敛：generation 恰好为 1，且只存一份定义。

    潜在缺陷类型：RLock 保护失效 / 检查-写入竞态导致重复计数或覆盖。
    """

    registry = ToolRegistry()
    barrier = threading.Barrier(8)

    def worker(index: int) -> None:
        barrier.wait()
        registry.register(_make_definition(description=f"d{index}"))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert registry.generation == 1
    assert registry.get_all_tool_names() == ["probe_tool"]


def test_constructor_registers_iterable_and_views_are_sorted() -> None:
    """构造期批量注册后，名称/定义视图按字典序稳定返回（既有契约不回归）。"""

    registry = ToolRegistry(
        [
            _make_definition(name="zeta"),
            _make_definition(name="alpha"),
            _make_definition(name="mid"),
        ]
    )

    assert registry.get_all_tool_names() == ["alpha", "mid", "zeta"]
    assert [definition.name for definition in registry.get_all_definitions()] == [
        "alpha",
        "mid",
        "zeta",
    ]
    assert registry.generation == 3


def test_get_schema_projects_live_provider_and_none_for_unknown() -> None:
    """``get_schema`` 命中时返回运行期投影，未注册时返回 None（既有契约不回归）。"""

    def provider() -> Mapping[str, Any]:
        return {"type": "object", "properties": {"live": {"type": "string"}}}

    registry = ToolRegistry([_make_definition(name="live_tool", schema_provider=provider)])

    assert registry.get_schema("live_tool") == provider()
    assert registry.get_schema("ghost_tool") is None


def test_deregister_and_permission_views_unchanged() -> None:
    """deregister 与权限投影的既有语义（只在命中时改动 generation；权限集合去重）。"""

    registry = ToolRegistry(
        [
            _make_definition(name="a", permission="safe_read"),
            _make_definition(name="b", permission="file_write"),
            _make_definition(name="c", permission="safe_read"),
        ]
    )
    assert registry.get_permissions() == {"safe_read", "file_write"}
    assert {definition.name for definition in registry.get_tools_by_permission("safe_read")} == {
        "a",
        "c",
    }
    assert registry.get_tools_by_permission("nope") == []

    registry.deregister("ghost_tool")
    assert registry.generation == 3, "移除不存在工具不得改变 generation"

    registry.deregister("b")
    assert registry.generation == 4
    assert registry.get_all_tool_names() == ["a", "c"]


# --------------------------------------------------------------------------- #
# 分组 3：WorkflowOperations.run_tool_calls（串行统一 to_thread / 未知工具 / 并行批次）
# --------------------------------------------------------------------------- #


def test_unknown_tool_name_yields_unknown_tool_observation_without_keyerror(
    tmp_path: Path,
) -> None:
    """未注册工具名仍须产出「未知工具」错误观察而非 KeyError（串行分支回归开关）。

    潜在缺陷类型：删除 ``_handler_kind_by_name`` 后调度层误用字典下标查名导致 KeyError。
    """

    registry = ToolRegistry(
        [_make_definition(name="known_tool", parameters_schema={"type": "object"})]
    )
    executor = ToolExecutor(registry=registry)
    context = ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=tmp_path, run_id=0)
    operations = _operations_with_executor(
        executor,
        parallel_modes={"known_tool": "serial"},
        allowed=frozenset({"known_tool"}),
        execution_context=context,
    )

    async def scenario() -> list[ToolObservation]:
        result = await operations.run_tool_calls(
            task_id=1,
            calls=[_call("ghost_tool", "call-ghost")],
            step_id="step-1",
        )
        return result.observations

    observations = asyncio.run(scenario())

    assert len(observations) == 1
    observation = observations[0]
    assert observation.status == "error"
    assert observation.error is not None
    assert observation.error == "unknown tool: ghost_tool"
    assert observation.reason is not None
    assert "not registered" in observation.reason
    assert observation.tool_call_id == "call-ghost"


def test_unknown_tool_alongside_known_serial_tool_keeps_order_and_count(
    tmp_path: Path,
) -> None:
    """同一批「已知串行工具 + 未知工具」：观察数量与串行输入顺序都必须保持。"""

    registry = ToolRegistry(
        [_make_definition(name="known_tool", parameters_schema={"type": "object"})]
    )
    executor = ToolExecutor(registry=registry)
    context = ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=tmp_path, run_id=0)
    operations = _operations_with_executor(
        executor,
        parallel_modes={"known_tool": "serial"},
        allowed=frozenset({"known_tool"}),
        execution_context=context,
    )

    async def scenario() -> list[ToolObservation]:
        result = await operations.run_tool_calls(
            task_id=1,
            calls=[
                _call("known_tool", "call-1", {"message": "hi"}),
                _call("ghost_tool", "call-2"),
                _call("known_tool", "call-3", {"message": "again"}),
            ],
            step_id="step-1",
        )
        return result.observations

    observations = asyncio.run(scenario())

    assert [observation.tool_call_id for observation in observations] == [
        "call-1",
        "call-2",
        "call-3",
    ]
    assert [observation.status for observation in observations] == ["success", "error", "success"]


def test_serial_batch_preserves_input_order_and_count() -> None:
    """纯串行批次：观察数量等于调用数，顺序等于入参顺序（串行分支不得乱序）。"""

    executor = _SleepingExecutor()
    operations = _operations_with_executor(
        executor,
        parallel_modes={"serial_tool": "serial"},
        allowed=frozenset({"serial_tool"}),
    )

    async def scenario() -> list[str]:
        result = await operations.run_tool_calls(
            task_id=1,
            calls=[_call("serial_tool", f"call-{index}") for index in range(4)],
            step_id="step-1",
        )
        return [observation.tool_call_id for observation in result.observations]

    assert asyncio.run(scenario()) == ["call-0", "call-1", "call-2", "call-3"]


def test_serial_batch_runs_off_the_event_loop_thread_and_keeps_loop_responsive() -> None:
    """串行调用必须经 to_thread 移出事件循环线程；期间事件循环仍能推进。

    潜在缺陷类型：串行分支回退为在 loop 线程上同步执行，阻塞 SSE / 取消接口。
    """

    executor = _SleepingExecutor({"call-0": 0.3, "call-1": 0.3})
    operations = _operations_with_executor(
        executor,
        parallel_modes={"serial_tool": "serial"},
        allowed=frozenset({"serial_tool"}),
    )
    loop_thread_ident = threading.get_ident()

    async def scenario() -> tuple[int, int]:
        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        pulse = asyncio.create_task(heartbeat())
        result = await operations.run_tool_calls(
            task_id=1,
            calls=[_call("serial_tool", "call-0"), _call("serial_tool", "call-1")],
            step_id="step-1",
        )
        ticks_during_batch = ticks
        pulse.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pulse
        return ticks_during_batch, len(result.observations)

    ticks_during_batch, observation_count = asyncio.run(scenario())

    assert observation_count == 2
    assert (
        ticks_during_batch >= 5
    ), f"串行批次期间事件循环只推进了 {ticks_during_batch} 次，说明仍在 loop 线程上同步执行"
    assert all(
        ident != loop_thread_ident for ident in executor.thread_idents
    ), "串行工具调用仍运行在事件循环线程上"


def test_internal_error_observation_when_executor_raises() -> None:
    """执行链内部异常必须收口为 error 观察（删除 async 分支后收口路径仍须成立）。"""

    class _RaisingExecutor:
        def execute(
            self,
            call: ToolCall,
            execution_context: object = None,
            allowed_tool_names: object = None,
        ) -> ToolObservation:
            del call, execution_context, allowed_tool_names
            raise RuntimeError("pipeline exploded")

    operations = _operations_with_executor(_RaisingExecutor())

    async def scenario() -> list[ToolObservation]:
        result = await operations.run_tool_calls(
            task_id=1,
            calls=[_call("boom_tool", "call-boom")],
            step_id="step-1",
        )
        return result.observations

    observations = asyncio.run(scenario())

    assert len(observations) == 1
    observation = observations[0]
    assert observation.status == "error"
    assert observation.retryable is False
    assert observation.tool_call_id == "call-boom"
    assert observation.error == "internal execution error before the tool ran: RuntimeError"


def test_parallel_batch_runs_concurrently_off_the_event_loop_thread() -> None:
    """parallel_mode="parallel" 的批次仍走线程池：真并发、不占事件循环线程、心跳可推进。"""

    executor = _SleepingExecutor({f"call-{index}": 0.2 for index in range(3)})
    operations = _operations_with_executor(
        executor,
        parallel_modes={"parallel_tool": "parallel"},
        allowed=frozenset({"parallel_tool"}),
    )
    loop_thread_ident = threading.get_ident()

    async def scenario() -> tuple[int, int]:
        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        pulse = asyncio.create_task(heartbeat())
        result = await operations.run_tool_calls(
            task_id=1,
            calls=[_call("parallel_tool", f"call-{index}") for index in range(3)],
            step_id="step-1",
        )
        ticks_during_batch = ticks
        pulse.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pulse
        return ticks_during_batch, len(result.observations)

    ticks_during_batch, observation_count = asyncio.run(scenario())

    assert observation_count == 3
    assert ticks_during_batch >= 5, "并行批次期间事件循环被占死"
    assert executor.max_active >= 2, "并行批次未并发执行（疑似退化为串行）"
    assert all(
        ident != loop_thread_ident for ident in executor.thread_idents
    ), "并行工具调用仍运行在事件循环线程上"
    assert sorted(executor.call_order) == ["call-0", "call-1", "call-2"]


def test_mixed_serial_and_parallel_batch_yields_one_observation_per_call() -> None:
    """串行 + 并行混合批次：每个调用恰好一条观察，无丢失、无重复。"""

    executor = _SleepingExecutor()
    operations = _operations_with_executor(
        executor,
        parallel_modes={"serial_tool": "serial", "parallel_tool": "parallel"},
        allowed=frozenset({"serial_tool", "parallel_tool"}),
    )

    async def scenario() -> list[str]:
        result = await operations.run_tool_calls(
            task_id=1,
            calls=[
                _call("serial_tool", "s-0"),
                _call("parallel_tool", "p-0"),
                _call("serial_tool", "s-1"),
                _call("parallel_tool", "p-1"),
            ],
            step_id="step-1",
        )
        return [observation.tool_call_id for observation in result.observations]

    observation_ids = asyncio.run(scenario())

    assert len(observation_ids) == 4
    assert set(observation_ids) == {"s-0", "s-1", "p-0", "p-1"}
    # 串行组按输入顺序排在前，并行组整体追加在后（既有契约）。
    assert observation_ids[:2] == ["s-0", "s-1"]


def test_run_tool_calls_does_not_write_handler_kind_state_on_instance() -> None:
    """门面实例上不得再存在 ``_handler_kind_by_name`` 调度状态（防半删残留字段）。"""

    operations = _operations_with_executor(
        _SleepingExecutor(),
        parallel_modes={"serial_tool": "serial"},
        allowed=frozenset({"serial_tool"}),
    )

    assert not hasattr(operations, "_handler_kind_by_name")


def test_empty_batch_returns_no_observations() -> None:
    """空批次边界：不得抛异常，返回空观察列表。"""

    executor = _SleepingExecutor()
    operations = _operations_with_executor(executor)

    async def scenario() -> list[ToolObservation]:
        result = await operations.run_tool_calls(task_id=1, calls=[], step_id="step-1")
        return result.observations

    assert asyncio.run(scenario()) == []
    assert executor.call_order == []


def test_parallel_helper_returns_empty_list_for_empty_calls() -> None:
    """``_run_calls_with_parallel_modes`` 空入参短路返回 []，不建线程池。"""

    executor = _SleepingExecutor()
    operations = _operations_with_executor(executor)

    async def scenario() -> list[tuple[int, ToolObservation]]:
        return await operations._run_calls_with_parallel_modes(1, [], "step-1")

    assert asyncio.run(scenario()) == []


def test_serial_batch_cancellation_propagates_without_waiting_for_handler() -> None:
    """串行分支经 to_thread：外层取消必须立即向调用方传播，不得被 handler 收尾阻塞。

    潜在缺陷类型：删除 async 通道后串行分支在 loop 线程上同步等待，取消被工具运行时阻塞。
    """

    executor = _SleepingExecutor({"call-0": 0.8})
    operations = _operations_with_executor(
        executor,
        parallel_modes={"serial_tool": "serial"},
        allowed=frozenset({"serial_tool"}),
    )

    async def scenario() -> float:
        started = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                operations.run_tool_calls(
                    task_id=1,
                    calls=[_call("serial_tool", "call-0")],
                    step_id="step-1",
                ),
                timeout=0.15,
            )
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 0.5


def test_run_tool_calls_binds_running_event_loop_into_execution_context(
    tmp_path: Path,
) -> None:
    """状态副作用：调用后 ``execution_context`` 的运行期依赖必须绑定到当前事件循环。

    串行统一走 to_thread 后，进程工具输出通道仍依赖这一绑定，不能因删除 async 分支而丢失。
    """

    executor = _SleepingExecutor()
    context = ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=tmp_path, run_id=0)
    operations = _operations_with_executor(
        executor,
        parallel_modes={"serial_tool": "serial"},
        allowed=frozenset({"serial_tool"}),
        execution_context=context,
    )
    assert context.runtime_dependencies.runtime_event_loop is None

    async def scenario() -> object:
        await operations.run_tool_calls(
            task_id=1,
            calls=[_call("serial_tool", "call-0")],
            step_id="step-1",
        )
        bound_context = operations._execution_context
        assert bound_context is not None
        return bound_context.runtime_dependencies.runtime_event_loop

    bound_loop = asyncio.run(scenario())

    assert bound_loop is not None
    assert isinstance(bound_loop, asyncio.AbstractEventLoop)


# --------------------------------------------------------------------------- #
# 分组 4：_is_parallel_call 改为只依据 parallel_mode
# --------------------------------------------------------------------------- #


def test_is_parallel_call_returns_false_for_unregistered_tool_name() -> None:
    """未在 ``_parallel_mode_by_name`` 中的工具名一律判为串行（不得抛 KeyError）。"""

    operations = _operations_with_executor(
        _SleepingExecutor(),
        parallel_modes={"parallel_tool": "parallel"},
    )

    assert operations._is_parallel_call(_call("ghost_tool", "call-1")) is False
    assert operations._is_parallel_call(_call("", "call-2")) is False


def test_is_parallel_call_true_only_for_parallel_mode() -> None:
    """``parallel_mode="parallel"`` 返回 True；"serial" 与空映射均返回 False。"""

    operations = _operations_with_executor(
        _SleepingExecutor(),
        parallel_modes={"parallel_tool": "parallel", "serial_tool": "serial"},
    )

    assert operations._is_parallel_call(_call("parallel_tool", "call-1")) is True
    assert operations._is_parallel_call(_call("serial_tool", "call-2")) is False
    assert operations._is_parallel_call(_call("unknown", "call-3")) is False


# --------------------------------------------------------------------------- #
# 分组 5：全仓残留符号扫描（生产代码树，排除 docs 计划文档）
# --------------------------------------------------------------------------- #


_REMOVED_SYMBOLS = (
    "handler_kind",
    "AsyncToolHandler",
    "execute_async",
    "_execute_inner_async",
    "_run_async_handler",
    "_wait_for_cancellation",
    "_await_cancelled_task",
    "_execute_async_tool_call",
    "_handler_kind_by_name",
    "_validate_handler_contract",
    "ToolCallCancelled",
    "build_cancellation_check",
    "cleanup_cancellation_signal",
    "parallel_group",
    "delegate_task_group",
)


def _find_symbol_references(roots: list[Path]) -> list[str]:
    """在给定根目录下的所有 ``*.py`` 中查找被删符号引用，返回 ``相对路径 :: 符号`` 列表。"""

    offenders: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for symbol in _REMOVED_SYMBOLS:
                if symbol in text:
                    offenders.append(f"{path} :: {symbol}")
    return offenders


def test_removed_symbol_scan_is_not_vacuous(tmp_path: Path) -> None:
    """自检：扫描器必须真的能检出被planted的符号，且扫描根确实覆盖到生产代码。

    潜在缺陷类型：扫描路径写错/为空导致「无残留」结论是假阳性。
    """

    (tmp_path / "planted.py").write_text("x = AsyncToolHandler\n", encoding="utf-8")

    assert _find_symbol_references([tmp_path]) == [f"{tmp_path / 'planted.py'} :: AsyncToolHandler"]

    scan_roots = [_BACKEND_ROOT / "app", _BACKEND_ROOT / "scripts"]
    assert all(root.is_dir() for root in scan_roots), "扫描根不存在，残留检查将形同虚设"
    scanned = sum(1 for root in scan_roots for _ in root.rglob("*.py"))
    assert scanned > 0, "扫描根下没有任何 .py 文件，残留检查将形同虚设"


def test_no_removed_symbols_remain_in_production_source_tree() -> None:
    """生产代码树（app/ 与 scripts/，排除 docs 计划文档）不得残留任何被删符号引用。

    潜在缺陷类型：删除不彻底的分支 / 注释 / 死代码仍在引用已删 API。
    """

    offenders = _find_symbol_references([_BACKEND_ROOT / "app", _BACKEND_ROOT / "scripts"])

    assert offenders == [], f"生产代码树仍残留被删符号引用: {offenders}"


def test_removed_executor_and_scheduler_attributes_are_absent() -> None:
    """被删的类成员必须真正不存在（防止以别名/兼容垫片复活）。"""

    for attribute in (
        "execute_async",
        "_execute_inner_async",
        "_run_async_handler",
        "_wait_for_cancellation",
        "_await_cancelled_task",
    ):
        assert not hasattr(ToolExecutor, attribute), f"ToolExecutor.{attribute} 仍存在"

    assert not hasattr(WorkflowOperations, "_execute_async_tool_call")
    assert not hasattr(ToolRegistry, "_validate_handler_contract")


# --------------------------------------------------------------------------- #
# 对抗性补充：删除注册期守卫后异步 handler 的静默后果
# --------------------------------------------------------------------------- #


def test_async_handler_is_silently_reported_as_success_at_execution(tmp_path: Path) -> None:
    """删除注册期协调器后，async handler 会被当作同步 handler 执行并「静默成功」。

    删除前：注册期即拒绝（handler_kind/协程不一致）。
    删除后：注册放行，执行期把未 await 的 coroutine 对象 ``str()`` 成 content，
    观察状态仍为 ``success``——错误被伪装成成功。

    本用例只记录现状（不作为本次改动引入的失败点），用于暴露该守卫缺失的残余风险。
    """

    async def async_handler(**kwargs: Any) -> ToolObservation:
        del kwargs
        return ToolObservation(tool_name="async_tool", status="success", content="real")

    registry = ToolRegistry(
        [
            _make_definition(
                name="async_tool", handler=async_handler, parameters_schema={"type": "object"}
            )
        ]
    )
    executor = ToolExecutor(registry=registry)
    context = ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=tmp_path, run_id=0)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        observation = executor.execute(
            _call("async_tool", "call-async"),
            execution_context=context,
            allowed_tool_names=frozenset({"async_tool"}),
        )
        # 强制回收未被 await 的协程，确保 RuntimeWarning 在捕获窗口内发出。
        gc.collect()

    assert observation.status == "success", "当前实现把未 await 的协程当作成功结果"
    assert observation.content is not None
    assert "<coroutine" in observation.content
    assert any(
        issubclass(warning.category, RuntimeWarning) for warning in caught
    ), "应至少发出「coroutine was never awaited」RuntimeWarning"


# --------------------------------------------------------------------------- #
# 分组 6：删除 execute_async 后 execute 仍是唯一入口 + 门面 __init__ 契约
# --------------------------------------------------------------------------- #


def test_tool_executor_read_surface_and_missing_definition(tmp_path: Path) -> None:
    """删除 async 入口后，执行器的只读面与缺失定义语义不变。"""

    definition = _make_definition(name="known_tool", parameters_schema={"type": "object"})
    executor = ToolExecutor(registry=ToolRegistry([definition]))

    assert [tool.name for tool in executor.list_tools()] == ["known_tool"]
    assert executor.get_tool_definition("known_tool") is not None
    assert executor.get_tool_definition("ghost_tool") is None
    # clear_task_state 只清进程内文件状态，对未登记 task 必须是静默 no-op。
    assert executor.clear_task_state(12345) is None


def test_tool_executor_execute_requires_execution_context() -> None:
    """``execute`` 是唯一执行入口：缺 execution_context 时必须抛 ValueError（装配错误）。"""

    executor = ToolExecutor(registry=ToolRegistry([_make_definition()]))

    with pytest.raises(ValueError, match="execution_context is required"):
        executor.execute(_call("probe_tool", "call-1"), execution_context=None)


def test_workflow_operations_init_derives_maps_without_handler_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``__init__`` 只派生 allowed/parallel_mode 两张表，不得再派生 handler_kind 表。"""

    monkeypatch.setattr(
        workflow_operations_module,
        "get_conversation_run_state_service",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        workflow_operations_module,
        "get_conversation_event_projector",
        lambda: SimpleNamespace(),
    )
    run = SimpleNamespace(id=7)
    task = SimpleNamespace(id=11)
    workspace = SimpleNamespace(id=13, root_path=".")

    operations = WorkflowOperations(
        tool_executor=ToolExecutor(
            registry=ToolRegistry(
                [
                    _make_definition(
                        name="serial_tool",
                        parameters_schema={"type": "object"},
                        parallel_mode="serial",
                    ),
                    _make_definition(
                        name="parallel_tool",
                        parameters_schema={"type": "object"},
                        parallel_mode="parallel",
                    ),
                ]
            )
        ),
        agent_profile=SimpleNamespace(agent_id="agent-1"),
        current_run=run,
        current_task=task,
        current_workspace=workspace,
        model_tools=[
            _make_definition(
                name="serial_tool", parameters_schema={"type": "object"}, parallel_mode="serial"
            ),
            _make_definition(
                name="parallel_tool", parameters_schema={"type": "object"}, parallel_mode="parallel"
            ),
        ],
    )

    assert operations._allowed_tool_names == frozenset({"serial_tool", "parallel_tool"})
    assert operations._parallel_mode_by_name == {
        "serial_tool": "serial",
        "parallel_tool": "parallel",
    }
    assert not hasattr(operations, "_handler_kind_by_name")
    assert operations.get_current_run() is run
    assert operations.get_current_task() is task
    assert operations.get_current_workspace() is workspace


def test_to_model_message_success_and_cancelled_branches() -> None:
    """观察 → ToolMessage 的成功/取消分支契约（不涉及本次改动，用于锁定模块行为）。"""

    operations = _operations_with_executor(_SleepingExecutor())

    success_empty = ToolObservation(tool_name="probe_tool", status="success", content="   ")
    message = operations.to_tool_model_message(success_empty)
    assert message.content == "success"
    assert message.status == "success"
    assert message.tool_call_id == ""

    success_body = ToolObservation(
        tool_name="probe_tool", status="success", content="body", tool_call_id="c-1"
    )
    message = operations.to_tool_model_message(success_body)
    assert message.content == "body"
    assert message.status == "success"
    assert message.tool_call_id == "c-1"

    cancelled = ToolObservation(
        tool_name="probe_tool",
        status="cancelled",
        reason="user stopped the run",
        tool_call_id="c-2",
    )
    message = operations.to_tool_model_message(cancelled)
    assert message.content == "cancelled: user stopped the run"
    # LangChain 不接受 cancelled，取消在消息层映射为 error 且保留语义标记。
    assert message.status == "error"

    cancelled_without_reason = ToolObservation(
        tool_name="probe_tool", status="cancelled", tool_call_id="c-3"
    )
    assert operations.to_tool_model_message(cancelled_without_reason).content == "cancelled"


def test_to_model_message_error_branch_keeps_diagnostic_fields() -> None:
    """错误观察 → ToolMessage：status 为 error，且保留 error/hint/reason 三要素。"""

    operations = _operations_with_executor(_SleepingExecutor())

    retryable_observation = ToolObservation(
        tool_name="probe_tool",
        status="error",
        error="provider unavailable",
        reason="retry after a short delay",
        retryable=True,
        tool_call_id="c-1",
    )
    message = operations.to_tool_model_message(retryable_observation)
    assert message.status == "error"
    assert message.tool_call_id == "c-1"
    assert message.content.startswith("error: provider unavailable\n")
    assert "hint: " in message.content
    assert message.content.endswith("reason: retry after a short delay")

    non_retryable_observation = ToolObservation(
        tool_name="probe_tool",
        status="error",
        error="bad patch",
        reason="fix the patch format",
        retryable=False,
        tool_call_id="c-2",
    )
    message = operations.to_tool_model_message(non_retryable_observation)
    assert "hint: do not retry this tool call." in message.content
    assert "reason: fix the patch format" in message.content


def test_process_event_swallows_projector_failure() -> None:
    """Transport 投影失败只能降级为 None，不得把异常带回工作流节点。"""

    class _FailingProjector:
        def process(self, event: object) -> object | None:
            raise RuntimeError("projector exploded")

    operations = _operations_with_executor(_SleepingExecutor())
    operations._event_projector = _FailingProjector()

    assert operations.process_event(SimpleNamespace(type="probe")) is None


def test_is_current_run_cancelled_prefers_injected_dependency() -> None:
    """取消判定：注入的 ``is_run_cancelled`` 优先，未注入时回退 run 级注册表。"""

    operations = _operations_with_executor(_SleepingExecutor())
    operations._current_run = SimpleNamespace(id=42)

    # 未绑定执行上下文 → 回退进程内 run 级取消注册表（未取消）。
    assert operations.is_current_run_cancelled() is False

    # 未绑定 run → 一律 False（无 run 可取消）。
    operations._current_run = None
    assert operations.is_current_run_cancelled() is False
    operations._current_run = SimpleNamespace(id=42)

    operations._execution_context = ToolExecutionContext(
        task_id=1,
        workspace_id=1,
        workspace_root=Path("."),
        run_id=42,
    )
    assert operations.is_current_run_cancelled() is False

    from app.core.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies

    operations._execution_context = ToolExecutionContext(
        task_id=1,
        workspace_id=1,
        workspace_root=Path("."),
        run_id=42,
        runtime_dependencies=ToolRuntimeDependencies(is_run_cancelled=lambda run_id: run_id == 42),
    )
    assert operations.is_current_run_cancelled() is True


def test_run_state_transitions_delegate_to_state_service() -> None:
    """终态迁移方法必须原样委托状态服务并透传参数（门面不得自行发事件）。"""

    recorded: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    class _RecordingStateService:
        def _record(self, name: str, *args: Any, **kwargs: Any) -> str:
            recorded.append((name, args, kwargs))
            return f"{name}:ok"

        def complete_run_if_running(self, *args: Any, **kwargs: Any) -> str:
            return self._record("complete", *args, **kwargs)

        def fail_run_if_running(self, *args: Any, **kwargs: Any) -> str:
            return self._record("fail", *args, **kwargs)

        def cancel_run_if_running(self, *args: Any, **kwargs: Any) -> str:
            return self._record("cancel", *args, **kwargs)

    operations = _operations_with_executor(_SleepingExecutor())
    operations._current_run = SimpleNamespace(id=99)
    operations._conversation_run_state_service = _RecordingStateService()

    assert operations.complete_run_if_running(final_output="done") == "complete:ok"
    assert operations.fail_run_if_running("boom", final_output="failed") == "fail:ok"
    assert operations.cancel_run_if_running(end_reason="user stopped") == "cancel:ok"

    assert recorded[0] == ("complete", (99,), {"final_output": "done", "usage_stats": None})
    assert recorded[1] == ("fail", (99, "boom"), {"final_output": "failed", "usage_stats": None})
    assert recorded[2] == (
        "cancel",
        (99, "user stopped"),
        {"final_output": None, "usage_stats": None},
    )
