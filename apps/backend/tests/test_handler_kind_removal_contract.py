"""``ToolDefinition.handler_kind`` / ``parallel_group`` 及相关调度通道删除后的缺陷发现型验证。

本次改动删除了：
- ``ToolDefinition.handler_kind`` 字段与 ``AsyncToolHandler`` Protocol；
- ``ToolExecutor.execute_async`` / ``_execute_inner_async`` / ``_run_async_handler`` /
  ``_wait_for_cancellation`` / ``_await_cancelled_task``，以及 ``execute`` 内
  ``handler_kind != "sync"`` 断言；
- 工具批次调度里的 ``_handler_kind_by_name`` / ``_execute_async_tool_call``（串行调用统一走
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
from typing import Any

import pytest
from pydantic import BaseModel

from app.core.tools.schemas import (
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.schemas import tool_definition as tool_definition_module
from app.core.tools.tool_execute.tool_executor import ToolExecutor
from app.core.tools.tool_registry import ToolRegistry
from app.core.workflow.node_helper import tool_batch_runner
from app.core.workflow.node_helper.tool_batch_runner import run_tool_batch

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
        "group": "probe",
        "description": "probe",
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
) -> SimpleNamespace:
    """只装配批次执行用到的运行上下文字段，不需要模型与数据库依赖。"""

    return SimpleNamespace(
        tool_executor=executor,
        execution_context=execution_context
        or ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=Path("."), run_id=0),
        allows_tools=frozenset(allowed or ()),
        tool_definitions={
            name: SimpleNamespace(parallel_mode=mode)
            for name, mode in (parallel_modes or {}).items()
        },
        task=SimpleNamespace(id=1),
    )


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
    field_names = {field.name for field in dataclasses.fields(ToolDefinition)}

    assert definition.parameters_schema == {}
    assert definition.timeout_seconds == 10.0
    assert definition.resource_keys == ()
    assert definition.display is None
    assert definition.execution_mode == "thread"
    assert definition.parallel_mode == "serial"
    assert "description_provider" not in field_names
    assert "schema_provider" not in field_names
    assert not hasattr(definition, "description_provider")
    assert not hasattr(definition, "schema_provider")


def test_tool_definition_requires_handler_and_args_model() -> None:
    """必填契约字段缺失时必须抛 TypeError（防止删除字段时顺手放宽构造约束）。"""

    with pytest.raises(TypeError):
        ToolDefinition(  # type: ignore[call-arg]
            name="missing_handler",
            group="probe",
            description="d",
            args_model=_ProbeArgs,
        )
    with pytest.raises(TypeError):
        ToolDefinition(  # type: ignore[call-arg]
            name="missing_args_model",
            group="probe",
            description="d",
            handler=_sync_probe_handler,
        )


def test_model_projection_exposes_strict_function_contract() -> None:
    """模型可见投影只含 strict function 契约，被删调度字段不得泄漏进下发结构。"""

    definition = _make_definition(parameters_schema={"type": "object", "properties": {}})

    projection = definition.to_model_tool_definition()

    assert set(projection) == {"name", "description", "parameters", "strict"}
    assert projection["strict"] is True
    assert projection["name"] == "probe_tool"


def test_normalized_derives_schema_when_no_parameters_schema() -> None:
    """无 parameters_schema 时，normalized() 用 args_model 派生 schema。"""

    definition = _make_definition()
    assert definition.parameters_schema == {}

    normalized = definition.normalized()

    assert normalized is not definition
    assert normalized.parameters_schema != {}
    assert normalized.parameters_schema == {
        **_ProbeArgs.model_json_schema(),
        "additionalProperties": False,
        "required": ["message"],
    }
    assert normalized.name == definition.name


def test_normalized_returns_self_when_parameters_schema_supplied() -> None:
    """已显式提供 parameters_schema 时 normalized() 必须原样返回自身（不固化、不复制）。"""

    schema = {"type": "object", "properties": {"message": {"type": "string"}}}
    definition = _make_definition(parameters_schema=schema)

    assert definition.normalized() is definition


def test_normalized_preserves_every_carried_contract_field() -> None:
    """normalized() 生成的副本必须逐字段保留调度/展示契约（防新副本漏字段）。"""

    definition = _make_definition(
        timeout_seconds=3.5,
        resource_keys=("filesystem", "shell"),
        execution_mode="process",
        parallel_mode="parallel",
    )

    normalized = definition.normalized()

    assert normalized.timeout_seconds == 3.5
    assert normalized.resource_keys == ("filesystem", "shell")
    assert normalized.execution_mode == "process"
    assert normalized.parallel_mode == "parallel"
    assert normalized.args_model is _ProbeArgs
    assert normalized.handler is definition.handler
    assert normalized.display is definition.display
    # 参数 schema 被派生，故二者不相等；派生后应幂等。
    assert normalized.normalized() == normalized


def test_normalized_keeps_parallel_mode_so_parallel_scheduling_is_not_lost() -> None:
    """parallel_mode 经 normalized() 后必须仍是 "parallel"（否则并行分组被静默降级为串行）。"""

    definition = _make_definition(name="parallel_tool", parallel_mode="parallel")

    normalized = definition.normalized()

    assert normalized.parallel_mode == "parallel"
    assert (
        normalized == _make_definition(name="parallel_tool", parallel_mode="parallel").normalized()
    )


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
    assert stored.parameters_schema == {
        **_ProbeArgs.model_json_schema(),
        "additionalProperties": False,
        "required": ["message"],
    }
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


def test_get_schema_returns_static_schema_and_none_for_unknown() -> None:
    """``get_schema`` 命中时返回静态 schema，未注册时返回 None。"""

    schema = {"type": "object", "properties": {"static": {"type": "string"}}}
    registry = ToolRegistry(
        [_make_definition(name="static_tool", parameters_schema=schema)]
    )

    assert registry.get_schema("static_tool") == schema
    assert registry.get_schema("ghost_tool") is None


# --------------------------------------------------------------------------- #
# 分组 3：run_tool_batch（串行统一 to_thread / 未知工具 / 并行批次）
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
        result = await run_tool_batch(operations, [_call("ghost_tool", "call-ghost")], "step-1")
        return result

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
        result = await run_tool_batch(operations, [
                _call("known_tool", "call-1", {"message": "hi"}),
                _call("ghost_tool", "call-2"),
                _call("known_tool", "call-3", {"message": "again"}),
            ], "step-1")
        return result

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
        result = await run_tool_batch(operations, [_call("serial_tool", f"call-{index}") for index in range(4)], "step-1")
        return [observation.tool_call_id for observation in result]

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
        result = await run_tool_batch(operations, [_call("serial_tool", "call-0"), _call("serial_tool", "call-1")], "step-1")
        ticks_during_batch = ticks
        pulse.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pulse
        return ticks_during_batch, len(result)

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
        result = await run_tool_batch(operations, [_call("boom_tool", "call-boom")], "step-1")
        return result

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
        result = await run_tool_batch(operations, [_call("parallel_tool", f"call-{index}") for index in range(3)], "step-1")
        ticks_during_batch = ticks
        pulse.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pulse
        return ticks_during_batch, len(result)

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
        result = await run_tool_batch(operations, [
                _call("serial_tool", "s-0"),
                _call("parallel_tool", "p-0"),
                _call("serial_tool", "s-1"),
                _call("parallel_tool", "p-1"),
            ], "step-1")
        return [observation.tool_call_id for observation in result]

    observation_ids = asyncio.run(scenario())

    assert len(observation_ids) == 4
    assert set(observation_ids) == {"s-0", "s-1", "p-0", "p-1"}
    # 串行组按输入顺序排在前，并行组整体追加在后（既有契约）。
    assert observation_ids[:2] == ["s-0", "s-1"]


def test_empty_batch_returns_no_observations() -> None:
    """空批次边界：不得抛异常，返回空观察列表。"""

    executor = _SleepingExecutor()
    operations = _operations_with_executor(executor)

    async def scenario() -> list[ToolObservation]:
        result = await run_tool_batch(operations, [], "step-1")
        return result

    assert asyncio.run(scenario()) == []
    assert executor.call_order == []


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
                run_tool_batch(operations, [_call("serial_tool", "call-0")], "step-1"),
                timeout=0.15,
            )
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 0.5


# --------------------------------------------------------------------------- #
# 分组 4：并行判定只依据 parallel_mode
# --------------------------------------------------------------------------- #


def test_is_parallel_call_returns_false_for_unregistered_tool_name() -> None:
    """未注册的工具名一律判为串行（不得抛 KeyError）。"""

    operations = _operations_with_executor(
        _SleepingExecutor(),
        parallel_modes={"parallel_tool": "parallel"},
    )

    assert tool_batch_runner._is_parallel(operations, _call("ghost_tool", "call-1")) is False
    assert tool_batch_runner._is_parallel(operations, _call("", "call-2")) is False


def test_is_parallel_call_true_only_for_parallel_mode() -> None:
    """``parallel_mode="parallel"`` 返回 True；"serial" 与空映射均返回 False。"""

    operations = _operations_with_executor(
        _SleepingExecutor(),
        parallel_modes={"parallel_tool": "parallel", "serial_tool": "serial"},
    )

    assert tool_batch_runner._is_parallel(operations, _call("parallel_tool", "call-1")) is True
    assert tool_batch_runner._is_parallel(operations, _call("serial_tool", "call-2")) is False
    assert tool_batch_runner._is_parallel(operations, _call("unknown", "call-3")) is False


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

    assert not hasattr(tool_batch_runner, "_execute_async_tool_call")
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


def test_emit_swallows_projector_failure() -> None:
    """事件 sink 失败只能记录降级，不得把异常带回工作流节点。"""
    from app.core.events import AssistantPartClosedEvent
    from app.core.workflow.node_helper.common import emit, execution_scope

    def failing_sink(_event: object) -> None:
        raise RuntimeError("projector exploded")

    with execution_scope(SimpleNamespace(event_sink=failing_sink), SimpleNamespace()):
        emit(AssistantPartClosedEvent(task_id=1, run_id=1, part="text"))


def test_is_cancelled_reads_task_signal() -> None:
    """取消判定只按当前 Task 读进程内信号：同一 Task 的执行串行，命中即当前执行。"""
    from app.core.workflow.cancel.conversation_run_cancellation_registry import (
        cancellation_registry,
    )
    from app.core.workflow.execution_state.runtime_context import RuntimeContext

    context = SimpleNamespace(task=SimpleNamespace(id=9_100_001))
    assert RuntimeContext.is_cancelled(context) is False
    cancellation_registry.request_cancel(9_100_001)
    try:
        assert RuntimeContext.is_cancelled(context) is True
    finally:
        cancellation_registry.consume(9_100_001)


