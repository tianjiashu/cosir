"""并行工具批次的等待必须离开事件循环线程。

本模块只覆盖一个契约：``WorkflowOperations._run_calls_with_parallel_modes`` 在等待并行
工具完成时**不得占用事件循环线程**。历史实现直接在协程内调用同步 ``concurrent.futures.wait``，
并行批次里只要有一个长工具（``delegate_task`` 可跑数分钟），整个后端 loop 就被占死：HTTP
全部无响应（前端「停止运行」的 ``POST /runs/{id}/cancel`` 根本到不了后端）、SSE 快照推送
停摆。因此这里用「心跳协程在批次期间是否能推进」作为判据，而不是只看返回值。
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import cast

import pytest

from app.config.constant import Constant
from app.core.observability.tool_trace_recorder import _NullToolTraceRecorder
from app.core.tools.schemas import ToolCall, ToolObservation
from app.core.tools.tool_execute.tool_executor import ToolExecutor
from app.core.workflows.workflow_operations import WorkflowOperations

_PARALLEL_TOOL = "fake_parallel_tool"


class _FakeToolExecutor:
    """按 ``call_id`` 睡眠的假执行器，同时记录并发峰值与启动顺序。

    Attributes:
        max_active: 批次内同时处于执行中的 worker 数峰值，用于验证并发上限。
        started_order: worker 实际开始执行的 ``call_id`` 序列。
        idle: 并发位归零（即所有已启动 worker 都已收尾）时置位的线程事件；供测试在
            离环线程里等待被放弃的 worker 收尾，避免忙等。
    """

    def __init__(self, delays: dict[str, float]) -> None:
        """记录每次调用的模拟耗时。

        参数:
            delays: ``call_id -> 睡眠秒数`` 映射；缺失的调用不睡眠。

        返回:
            无（构造函数）。

        异常:
            无。

        副作用:
            初始化并发计数、启动顺序记录与空闲事件。
        """
        self._delays = delays
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.started_order: list[str] = []
        self.idle = threading.Event()

    def execute(
        self,
        call: ToolCall,
        execution_context: object = None,
        allowed_tool_names: object = None,
    ) -> ToolObservation:
        """模拟一次同步阻塞的工具执行。

        参数:
            call: 本次工具调用；只用其 ``call_id`` 与 ``tool_name``。
            execution_context: 假执行器忽略。
            allowed_tool_names: 假执行器忽略。

        返回:
            携带 ``call_id`` 的 success 观察，便于调用方校验结果与调用的对应关系。

        异常:
            无。

        副作用:
            占用一个并发位并在其 ``delays`` 时长内阻塞当前 worker 线程。
        """
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started_order.append(call.call_id)
        try:
            time.sleep(self._delays.get(call.call_id, 0.0))
        finally:
            with self._lock:
                self.active -= 1
                if self.active == 0:
                    self.idle.set()
        return ToolObservation(
            tool_name=call.tool_name,
            status="success",
            content=call.call_id,
            tool_call_id=call.call_id,
        )


def _make_operations(executor: _FakeToolExecutor) -> WorkflowOperations:
    """装配只依赖执行器的 ``WorkflowOperations`` 门面。

    参数:
        executor: 假工具执行器。

    返回:
        属性已注入、可直接调用 ``run_tool_calls`` 的门面实例；其余协作者（模型、run、task）
        与本模块的并行调度契约无关，故不装配。

    异常:
        无。

    副作用:
        无；不触碰数据库、日志之外的全局状态。
    """
    operations = WorkflowOperations.__new__(WorkflowOperations)
    # 门面声明的 ``_executor`` 是真实 ``ToolExecutor``；本模块只消费它的 ``execute`` 语义，
    # 故用 ``cast`` 注入替身，而不是为假执行器伪造整条执行管线。
    operations._executor = cast(ToolExecutor, executor)
    operations._trace_recorder = _NullToolTraceRecorder()
    operations._execution_context = None
    operations._allowed_tool_names = frozenset({_PARALLEL_TOOL})
    operations._parallel_mode_by_name = {_PARALLEL_TOOL: "parallel"}
    return operations


def _parallel_call(index: int) -> ToolCall:
    """构造一个声明为并行工具名的工具调用。

    参数:
        index: 用于生成稳定 ``call_id`` 的下标。

    返回:
        ``call_id=call-<index>`` 的 ``ToolCall``。

    异常:
        无。

    副作用:
        无。
    """
    return ToolCall(tool_name=_PARALLEL_TOOL, arguments={}, call_id=f"call-{index}")


def test_parallel_batch_does_not_block_event_loop() -> None:
    """并行批次执行期间事件循环仍必须能推进（本缺陷的判据测试）。

    心跳协程与工具批次竞争同一个事件循环：修复前批次在 loop 线程上同步等 worker，
    心跳一次都跑不动（tick == 0）；修复后心跳应持续推进。
    """
    executor = _FakeToolExecutor({"call-0": 0.3, "call-1": 0.3})
    operations = _make_operations(executor)

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
            calls=[_parallel_call(0), _parallel_call(1)],
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
    ), f"并行批次期间事件循环只推进了 {ticks_during_batch} 次，说明等待仍在 loop 线程上"


def test_parallel_batch_respects_constant_max_parallel_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """在途 worker 数不得超过 ``Constant.Tools.MAX_PARALLEL_CALLS``，且每个调用都产出观察。"""
    monkeypatch.setattr(Constant.Tools, "MAX_PARALLEL_CALLS", 2)
    executor = _FakeToolExecutor({f"call-{index}": 0.15 for index in range(4)})
    operations = _make_operations(executor)

    async def scenario() -> int:
        result = await operations.run_tool_calls(
            task_id=1,
            calls=[_parallel_call(index) for index in range(4)],
            step_id="step-1",
        )
        return len(result.observations)

    assert asyncio.run(scenario()) == 4
    assert executor.max_active == 2
    assert sorted(executor.started_order) == [f"call-{index}" for index in range(4)]


def test_parallel_batch_submits_pending_calls_in_argument_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """额度满时剩余调用按入参顺序排队提交，而不是逆序。"""
    monkeypatch.setattr(Constant.Tools, "MAX_PARALLEL_CALLS", 2)
    executor = _FakeToolExecutor({"call-0": 0.1, "call-1": 0.3, "call-2": 0.05})
    operations = _make_operations(executor)

    async def scenario() -> int:
        result = await operations.run_tool_calls(
            task_id=1,
            calls=[_parallel_call(0), _parallel_call(1), _parallel_call(2)],
            step_id="step-1",
        )
        return len(result.observations)

    assert asyncio.run(scenario()) == 3
    # 前两个 worker 的启动先后由线程调度决定，故只比较集合；第三个必须是入参顺序里的 call-2。
    assert set(executor.started_order[:2]) == {"call-0", "call-1"}
    assert executor.started_order[2] == "call-2"


def test_parallel_batch_results_follow_completion_order() -> None:
    """观察顺序保持「实际完成顺序」契约（未按原始下标重排）。"""
    executor = _FakeToolExecutor({"call-0": 0.3, "call-1": 0.05})
    operations = _make_operations(executor)

    async def scenario() -> list[str]:
        result = await operations.run_tool_calls(
            task_id=1,
            calls=[_parallel_call(0), _parallel_call(1)],
            step_id="step-1",
        )
        return [observation.tool_call_id for observation in result.observations]

    assert asyncio.run(scenario()) == ["call-1", "call-0"]


def test_parallel_batch_cancellation_returns_without_waiting_for_workers() -> None:
    """取消必须立刻向调用方传播，不能被 worker 收尾（``shutdown(wait=True)``）拖住。"""
    executor = _FakeToolExecutor({"call-0": 1.0, "call-1": 1.0})
    operations = _make_operations(executor)

    async def scenario() -> float:
        executor.idle.clear()
        started = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                operations.run_tool_calls(
                    task_id=1,
                    calls=[_parallel_call(0), _parallel_call(1)],
                    step_id="step-1",
                ),
                timeout=0.2,
            )
        elapsed = time.monotonic() - started
        # 等被放弃的 worker 真正收尾：loop 仍存活时回收才安全（否则 ``wrap_future`` 的残留
        # 回调会打向已关闭的 loop）。用执行器空闲事件在离环线程里等，既不用固定 sleep
        # （会与 worker 时长耦合），也不忙等事件循环。
        settled = await asyncio.to_thread(executor.idle.wait, 5.0)
        assert settled, "被放弃的 worker 未在预期时间内收尾"
        return elapsed

    assert asyncio.run(scenario()) < 0.6
