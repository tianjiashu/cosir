"""验证修复 B：并行工具 worker 继承调用线程 contextvars（OTel current context）。

背景：``_run_calls_with_parallel_modes`` 每次线程池提交用
``contextvars.copy_context().run`` 包装，使 ``delegate_task`` 等并行工具的
OTel current span 嵌套在父 turn trace 下，而不是脱离为独立 trace。

覆盖契约：
1. 独立验证 ``contextvars.copy_context().run`` 能把调用线程的 ContextVar 值
   带进 ``ThreadPoolExecutor`` worker（对照：不包装则读到默认值）。
2. service 层：并行 worker 内能读到调用线程设置的 ContextVar（修复 B 核心）。
3. 每次提交是独立 context 副本：worker 内修改不回流调用线程，也不跨任务串扰。
4. 并行组取消路径不受 contextvars 包装影响（占位闭合、已执行任务仍能读 context）。
"""

import contextvars
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

from pydantic import BaseModel

from app.service.tool_execution.tool_execution_service import ToolExecutionService
from app.tools.schemas import ToolCall, ToolDefinition, ToolObservation
from app.tools.tool_execute.tool_scheduler import ToolScheduler

# 测试专用 ContextVar：模拟 OTel current context 的传播语义。
_test_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_parallel_ctx_test", default="<unset>"
)


class _EmptyArgs(BaseModel):
    """测试用最小工具参数模型。"""


def _make_call(call_id: str, tool_name: str = "parallel_tool") -> ToolCall:
    return ToolCall(call_id=call_id, tool_name=tool_name, arguments={})


def _definition(name: str, parallel: bool) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=name,
        permission="tool_read",
        handler=lambda: None,
        args_model=_EmptyArgs,
        parallel_mode="parallel" if parallel else "serial",
    )


def _success_observation(call: ToolCall) -> ToolObservation:
    return ToolObservation(
        tool_name=call.tool_name,
        status="success",
        content=f"ok:{call.call_id}",
        error="",
        reason="",
        retryable=False,
        tool_call_id=call.call_id,
    )


def _make_service(
    scheduler: ToolScheduler,
    should_cancel: Callable[[], bool] | None = None,
) -> ToolExecutionService:
    return ToolExecutionService(
        scheduler=scheduler,
        agent_id="test-agent",
        should_cancel=should_cancel,
        trace_recorder=MagicMock(),
        tool_definitions=[_definition("parallel_tool", parallel=True)],
    )


def test_contextvars_copy_context_run_propagates_into_worker() -> None:
    """测试目的：独立验证 copy_context().run 把调用线程 ContextVar 带入 worker；
    对照不包装时 worker 读到默认值。

    可能发现的缺陷：修复 B 未使用 copy_context（或错误复用了同一 Context 对象）导致
    worker 读不到父线程 context。
    """
    _test_ctx.set("parent-value")
    results: dict[str, str] = {}

    def _read_value() -> None:
        results["wrapped"] = _test_ctx.get()
        # 主线程的值不受 worker 影响
        results["caller_after"] = _test_ctx.get()

    with ThreadPoolExecutor(max_workers=1) as pool:
        wrapped_future = pool.submit(contextvars.copy_context().run, _read_value)
        wrapped_future.result()

    # 对照：不包装时 worker 在全新 context 运行，读到默认值
    with ThreadPoolExecutor(max_workers=1) as pool:
        unwrapped_future = pool.submit(lambda: results.__setitem__("unwrapped", _test_ctx.get()))
        unwrapped_future.result()

    assert results["wrapped"] == "parent-value", "copy_context 包装后 worker 应读到父线程值"
    assert results["unwrapped"] == "<unset>", "不包装时 worker 应读到默认值（对照成立）"
    assert results["caller_after"] == "parent-value"


def test_parallel_worker_reads_calling_thread_contextvar() -> None:
    """测试目的：service 并行路径下，worker 内能读到调用线程设置的 ContextVar。

    可能发现的缺陷：修复 B 缺失导致 delegate_task 等并行工具 worker 丢失父 turn
    OTel current context（trace 脱离父级）。
    """
    _test_ctx.set("turn-otel-ctx")
    observed: dict[str, str] = {}
    lock = threading.Lock()

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        with lock:
            observed[call.call_id] = _test_ctx.get()
        return _success_observation(call)

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    service = _make_service(scheduler, should_cancel=lambda: False)

    calls = [_make_call("p1"), _make_call("p2"), _make_call("p3")]
    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=MagicMock(),
    )

    assert [o.status for o in result.observations] == ["success"] * 3
    assert observed == {
        "p1": "turn-otel-ctx",
        "p2": "turn-otel-ctx",
        "p3": "turn-otel-ctx",
    }, "并行 worker 必须继承调用线程 contextvars"


def test_parallel_worker_context_mutation_does_not_leak_back() -> None:
    """测试目的：每个提交持有独立 context 副本，worker 内 set 不回流调用线程。

    可能发现的缺陷：提交复用了同一个 Context 对象被并发进入（文档明确禁止），或
    worker 修改污染主线程 context。
    """
    _test_ctx.set("original")
    worker_value = {"set": False}

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        _test_ctx.set("mutated-in-worker")
        worker_value["set"] = True
        return _success_observation(call)

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    service = _make_service(scheduler, should_cancel=lambda: False)

    calls = [_make_call("p1"), _make_call("p2")]
    service.run_calls_with_events(step_id="s1", calls=calls, write_event=MagicMock())

    assert worker_value["set"] is True
    assert _test_ctx.get() == "original", "worker 内 context 修改不得回流调用线程"


def test_parallel_cancellation_unaffected_by_context_wrapping() -> None:
    """测试目的：contextvars 包装不改变并行组取消语义——取消后整体跳过补占位，
    已执行任务仍能读到调用线程 context。

    可能发现的缺陷：引入 copy_context 包装后取消检查/占位逻辑回归（配对不闭合）。
    """
    _test_ctx.set("ctx-for-cancel-test")
    executed: dict[str, str] = {}
    lock = threading.Lock()

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        with lock:
            executed[call.call_id] = _test_ctx.get()
        return _success_observation(call)

    state = {"cancelled": True}

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    service = _make_service(scheduler, should_cancel=lambda: state["cancelled"])

    calls = [_make_call("p1"), _make_call("p2")]
    result = service.run_calls_with_events(step_id="s1", calls=calls, write_event=MagicMock())

    # 取消在并行批前生效：全部跳过补占位，配对闭合
    assert executed == {}, "取消生效时并行组应整体跳过"
    assert [o.status for o in result.observations] == ["cancelled", "cancelled"]
    produced_ids = {m.metadata.get("tool_call_id") for m in result.messages_for_model}
    assert produced_ids == {"p1", "p2"}

    # 对照：不取消时执行，且 worker 能读到调用线程 context（证明取消是唯一跳过原因）
    state["cancelled"] = False
    result_ok = service.run_calls_with_events(step_id="s1", calls=calls, write_event=MagicMock())
    assert [o.status for o in result_ok.observations] == ["success", "success"]
    assert executed == {"p1": "ctx-for-cancel-test", "p2": "ctx-for-cancel-test"}
