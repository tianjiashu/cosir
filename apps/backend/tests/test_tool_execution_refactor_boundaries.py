"""独立验证「并行/串行分流重构」的边界行为（不修改任何业务代码）。

覆盖目标：``run_calls_with_events`` 按 ``_is_parallel_call`` 分流后，纯串行批、
纯并行批、并行 worker 内部异常、防御分支、空批、事件副作用与 output sink
的边界行为必须与重构前语义完全一致。本文件只读取 ``apps/backend/app/**``
业务代码，不修改它们。
"""

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from app.models.enums.event_type import EventType
from app.models.payload.file_change_updated_payload import FileChangeUpdatedPayload
from app.service.agent_runtime_event.runtime_event_bus import RuntimeEventBus
from app.service.tool_execution.run_result import ToolRunResult
from app.service.tool_execution.tool_execution_service import ToolExecutionService
from app.tools.schemas import (
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.tools.tool_execute.tool_scheduler import ToolScheduler


class _EmptyArgs(BaseModel):
    """测试用最小工具参数模型（service 层不执行 handler）。"""


def _make_call(call_id: str, tool_name: str = "read_file") -> ToolCall:
    return ToolCall(
        call_id=call_id,
        tool_name=tool_name,
        arguments={"path": f"/workspace/{call_id}.txt"},
    )


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
        content="ok",
        error="",
        reason="",
        retryable=False,
        tool_call_id=call.call_id,
    )


def _make_service(
    scheduler: ToolScheduler,
    should_cancel: Callable[[], bool] | None = None,
    tool_definitions: list[ToolDefinition] | None = None,
    event_bus: RuntimeEventBus | None = None,
) -> ToolExecutionService:
    return ToolExecutionService(
        scheduler=scheduler,
        agent_id="test-agent",
        should_cancel=should_cancel,
        trace_recorder=MagicMock(),
        tool_definitions=tool_definitions,
        event_bus=event_bus,
    )


def _recording_write_event(events: list[tuple[EventType, str]]) -> Callable[..., None]:
    def _write(event_type: EventType, payload: object) -> None:
        events.append((event_type, payload.tool_call_id))  # type: ignore[attr-defined]

    return _write


def _assert_pairing_closed(calls: list[ToolCall], result: ToolRunResult) -> None:
    assert len(result.observations) == len(calls)
    produced_ids = {
        m.metadata.get("tool_call_id") for m in result.messages_for_model if m.role == "tool"
    }
    expected_ids = {c.call_id for c in calls}
    assert (
        produced_ids == expected_ids
    ), f"占位不闭合：produced={produced_ids}, expected={expected_ids}"


def test_pure_serial_batch_explicit_serial_mode_runs_in_order() -> None:
    """显式声明 serial 模式的纯串行批：严格按序执行、单线程、配对闭合。

    可能发现的缺陷：``_is_parallel_call`` 误判 serial 为 parallel 导致串行组
    被错误送进并行路径（顺序/线程语义被破坏）。
    """
    tool_definitions = [_definition("serial_tool", parallel=False)]
    calls = [
        _make_call("s1", "serial_tool"),
        _make_call("s2", "serial_tool"),
        _make_call("s3", "serial_tool"),
    ]
    execution_order: list[str] = []
    thread_ids: list[int] = []

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        execution_order.append(call.call_id)
        thread_ids.append(threading.get_ident())
        return _success_observation(call)

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    service = _make_service(
        scheduler, should_cancel=lambda: False, tool_definitions=tool_definitions
    )

    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=MagicMock(),
    )

    assert execution_order == ["s1", "s2", "s3"]
    assert len(set(thread_ids)) == 1, "串行 call 被并发执行（疑似误入并行路径）"
    assert [o.tool_call_id for o in result.observations] == ["s1", "s2", "s3"]
    assert all(o.status == "success" for o in result.observations)
    _assert_pairing_closed(calls, result)


def test_pure_parallel_batch_all_success_pairing_closed() -> None:
    """纯并行批全部成功：观察按原始 index 排序、配对闭合、无多余占位。

    可能发现的缺陷：并行组结果未按原始 index 统一排序 / 观察丢失导致
    配对不闭合 / 取消信号被误触发。
    """
    tool_definitions = [_definition("parallel_tool", parallel=True)]
    calls = [
        _make_call("p1", "parallel_tool"),
        _make_call("p2", "parallel_tool"),
        _make_call("p3", "parallel_tool"),
    ]

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        return _success_observation(call)

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    service = _make_service(
        scheduler, should_cancel=lambda: False, tool_definitions=tool_definitions
    )

    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=MagicMock(),
    )

    assert [o.tool_call_id for o in result.observations] == ["p1", "p2", "p3"]
    assert all(o.status == "success" for o in result.observations)
    _assert_pairing_closed(calls, result)


def test_pure_parallel_batch_write_event_exception_contained() -> None:
    """纯并行批 write_event 抛异常：不穿透、工具照常执行、观察不降级。

    可能发现的缺陷：并行路径事件写入异常穿透 ``_emit_event_safely`` /
    ``_run_calls_with_parallel_modes`` 的收口，导致批次中断或观察被降级为 error。
    """
    tool_definitions = [_definition("parallel_tool", parallel=True)]
    calls = [
        _make_call("p1", "parallel_tool"),
        _make_call("p2", "parallel_tool"),
        _make_call("p3", "parallel_tool"),
    ]

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        return _success_observation(call)

    def _exploding_write_event(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("event bus unavailable")

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    service = _make_service(
        scheduler, should_cancel=lambda: False, tool_definitions=tool_definitions
    )

    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=_exploding_write_event,
    )

    assert len(result.observations) == len(
        calls
    ), f"配对不闭合：observations={len(result.observations)}, calls={len(calls)}"
    _assert_pairing_closed(calls, result)
    assert all(
        o.status == "success" for o in result.observations
    ), "事件通道故障不应把并行正常执行的观察降级为 error"


def test_parallel_worker_internal_error_collected_as_error_observation() -> None:
    """并行组 worker 执行链抛异常：对应 call 收口为 error 占位，其余不受影响。

    可能发现的缺陷：并行 worker 的异常未被 ``_execute_tool_call`` 收口，
    导致 ``future.result()`` 穿透或配对断开。
    """
    tool_definitions = [_definition("parallel_tool", parallel=True)]
    calls = [
        _make_call("p1", "parallel_tool"),
        _make_call("p2", "parallel_tool"),
        _make_call("p3", "parallel_tool"),
    ]

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        if call.call_id == "p2":
            raise RuntimeError("bug in parallel scheduler internal logic")
        return _success_observation(call)

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    service = _make_service(
        scheduler, should_cancel=lambda: False, tool_definitions=tool_definitions
    )

    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=MagicMock(),
    )

    _assert_pairing_closed(calls, result)
    by_id = {o.tool_call_id: o for o in result.observations}
    assert by_id["p1"].status == "success"
    assert by_id["p3"].status == "success"
    assert by_id["p2"].status == "error"
    assert "internal execution error" in by_id["p2"].error
    assert by_id["p2"].retryable is False


def test_parallel_batch_future_result_exception_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``future.result()`` 防御分支：worker 抛出的意外异常被收口为 error 观察。

    直接替换 ``_execute_tool_call`` 模拟 worker 线程未收口、由
    ``_run_calls_with_parallel_modes`` 的 ``except`` 兜底收口的路径。
    可能发现的缺陷：防御分支缺失导致 ``future.result()`` 异常穿透批次。
    """
    tool_definitions = [_definition("parallel_tool", parallel=True)]
    calls = [
        _make_call("p1", "parallel_tool"),
        _make_call("p2", "parallel_tool"),
    ]
    scheduler = MagicMock(spec=ToolScheduler)

    def _boom(*_args: object, **_kwargs: object) -> ToolObservation:
        raise RuntimeError("unhandled in worker")

    service = _make_service(
        scheduler, should_cancel=lambda: False, tool_definitions=tool_definitions
    )
    service._execute_tool_call = _boom  # type: ignore[method-assign]

    caplog.set_level(logging.ERROR)
    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=MagicMock(),
    )

    _assert_pairing_closed(calls, result)
    assert all(o.status == "error" for o in result.observations)
    assert all("internal execution error" in o.error for o in result.observations)


def test_empty_batch_returns_empty_result() -> None:
    """空批（calls=[]）返回空结果：不崩溃、不产生事件、无占位。

    可能发现的缺陷：分流逻辑对空列表的边界处理（除零/越界/误发事件）。
    """
    scheduler = MagicMock(spec=ToolScheduler)
    service = _make_service(scheduler, should_cancel=lambda: False)

    events: list[tuple[EventType, str]] = []
    result = service.run_calls_with_events(
        step_id="s1",
        calls=[],
        write_event=_recording_write_event(events),
    )

    assert result.observations == []
    assert result.messages_for_model == []
    assert events == []
    scheduler.execute.assert_not_called()


def test_parallel_batch_emits_started_then_finished_per_call() -> None:
    """并行批事件副作用：每个 call 恰好一条 STARTED + 一条 FINISHED，且同 call
    STARTED 先于 FINISHED。

    可能发现的缺陷：事件泄漏/重复/缺失（如 started 只在 submit 时发而
    finished 丢失）、并行路径事件侧效应错乱。
    """
    tool_definitions = [_definition("parallel_tool", parallel=True)]
    calls = [
        _make_call("p1", "parallel_tool"),
        _make_call("p2", "parallel_tool"),
    ]

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        return _success_observation(call)

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    service = _make_service(
        scheduler, should_cancel=lambda: False, tool_definitions=tool_definitions
    )

    events: list[tuple[EventType, str]] = []
    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=_recording_write_event(events),
    )

    _assert_pairing_closed(calls, result)
    started = [cid for etype, cid in events if etype == EventType.TOOL_CALL_STARTED]
    finished = [cid for etype, cid in events if etype == EventType.TOOL_CALL_FINISHED]
    assert sorted(started) == ["p1", "p2"]
    assert sorted(finished) == ["p1", "p2"]
    for call in calls:
        positions = [i for i, (etype, cid) in enumerate(events) if cid == call.call_id]
        started_idx = next(i for i in positions if events[i][0] == EventType.TOOL_CALL_STARTED)
        finished_idx = next(i for i in positions if events[i][0] == EventType.TOOL_CALL_FINISHED)
        assert started_idx < finished_idx


def test_is_parallel_call_lookup_rules() -> None:
    """``_is_parallel_call`` 判定规则：parallel→True、serial→False、未定义→False。

    可能发现的缺陷：判定键/默认值错误（未定义工具被误判为 parallel，或
    serial 定义被当作并行）。
    """
    tool_definitions = [
        _definition("parallel_tool", parallel=True),
        _definition("serial_tool", parallel=False),
    ]
    scheduler = MagicMock(spec=ToolScheduler)
    service = _make_service(
        scheduler, should_cancel=lambda: False, tool_definitions=tool_definitions
    )

    assert service._is_parallel_call(_make_call("p1", "parallel_tool")) is True
    assert service._is_parallel_call(_make_call("s1", "serial_tool")) is False
    assert service._is_parallel_call(_make_call("u1", "unknown_tool")) is False


def test_parallel_modes_empty_or_cancelled_returns_empty() -> None:
    """私有方法防御分支：空/取消时并行执行器返回空列表，不启动线程池。

    可能发现的缺陷：防御分支缺失导致空列表崩溃或取消被忽略仍启动 worker。
    """
    tool_definitions = [_definition("parallel_tool", parallel=True)]
    scheduler = MagicMock(spec=ToolScheduler)
    write_event = MagicMock()

    service = _make_service(
        scheduler, should_cancel=lambda: False, tool_definitions=tool_definitions
    )
    assert (
        service._run_calls_with_parallel_modes(
            step_id="s1",
            calls=[],
            execution_context=None,
            write_event=write_event,
            loop=None,
        )
        == []
    )
    service_cancelled = _make_service(
        scheduler, should_cancel=lambda: True, tool_definitions=tool_definitions
    )
    assert (
        service_cancelled._run_calls_with_parallel_modes(
            step_id="s1",
            calls=[(0, _make_call("p1", "parallel_tool"))],
            execution_context=None,
            write_event=write_event,
            loop=None,
        )
        == []
    )
    scheduler.execute.assert_not_called()


def _context(task_id: str, turn_id: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        task_id=task_id,
        workspace_id="w1",
        workspace_root=Path("."),
        turn_id=turn_id,
    )


def test_output_sink_built_only_with_bus_and_loop() -> None:
    """``_build_output_sink``：缺 event_bus/loop 时返回 None；具备时构造 sink 且
    调度回环；``call_soon_threadsafe`` 抛 RuntimeError 时降级不向上抛。

    可能发现的缺陷：实时通道在缺上下文时仍构造 sink（无处可发）/ 调度异常
    穿透命令执行。
    """
    captured: list[object | None] = []

    def _scheduler_execute(call: ToolCall, **kwargs: object) -> ToolObservation:
        captured.append(kwargs.get("output_sink"))
        return _success_observation(call)

    # 场景 A：缺 event_bus → sink 为 None
    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    service_no_bus = _make_service(scheduler, should_cancel=lambda: False)
    service_no_bus.run_calls_with_events(
        step_id="s1",
        calls=[_make_call("s1")],
        execution_context=_context("t1", "turn1"),
        write_event=MagicMock(),
    )
    assert captured and captured[0] is None

    # 场景 B：event_bus + loop 正常 → sink 非 None，且调度回环
    captured.clear()
    scheduler_b = MagicMock(spec=ToolScheduler)
    scheduler_b.execute.side_effect = _scheduler_execute
    bus_b = MagicMock(spec=RuntimeEventBus)
    loop_b = MagicMock()
    service_b = _make_service(scheduler_b, should_cancel=lambda: False, event_bus=bus_b)
    service_b.run_calls_with_events(
        step_id="s1",
        calls=[_make_call("s1")],
        execution_context=_context("t1", "turn1"),
        write_event=MagicMock(),
        running_loop=loop_b,
    )
    sink_b = cast(Callable[[str, bool], None], captured[0])
    sink_b("hello", False)
    loop_b.call_soon_threadsafe.assert_called_once()

    # 场景 C：call_soon_threadsafe 抛 RuntimeError → 降级不向上抛
    captured.clear()
    scheduler_c = MagicMock(spec=ToolScheduler)
    scheduler_c.execute.side_effect = _scheduler_execute
    bus_c = MagicMock(spec=RuntimeEventBus)
    loop_c = MagicMock()
    loop_c.call_soon_threadsafe.side_effect = RuntimeError("loop closed")
    service_c = _make_service(scheduler_c, should_cancel=lambda: False, event_bus=bus_c)
    service_c.run_calls_with_events(
        step_id="s1",
        calls=[_make_call("s1")],
        execution_context=_context("t1", "turn1"),
        write_event=MagicMock(),
        running_loop=loop_c,
    )
    sink_c = cast(Callable[[str, bool], None], captured[0])
    sink_c("hello", False)  # 不抛


def test_parallel_batch_publishes_file_change_updated() -> None:
    """并行批成功且产生文件变更时，经 event_bus 广播 FILE_CHANGE_UPDATED。

    覆盖并行路径 ``_handle_completed_observation`` 内的实时广播分支
    （``_publish_file_change_updated`` 的 diff 统计与逐文件调度）。
    可能发现的缺陷：并行完成路径漏广播 / 广播异常穿透执行。
    """
    tool_definitions = [_definition("parallel_tool", parallel=True)]
    calls = [
        _make_call("p1", "parallel_tool"),
        _make_call("p2", "parallel_tool"),
    ]

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        return ToolObservation(
            tool_name=call.tool_name,
            status="success",
            content="ok",
            error="",
            reason="",
            retryable=False,
            tool_call_id=call.call_id,
            data={
                "changes": [{"path": "a.txt", "status": "modified", "before": "1", "after": "2"}]
            },
        )

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    bus = MagicMock(spec=RuntimeEventBus)
    loop = MagicMock()
    service = _make_service(
        scheduler,
        should_cancel=lambda: False,
        tool_definitions=tool_definitions,
        event_bus=bus,
    )

    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        execution_context=_context("t1", "turn1"),
        write_event=MagicMock(),
        running_loop=loop,
    )

    _assert_pairing_closed(calls, result)
    assert all(o.status == "success" for o in result.observations)
    # 每个成功 call 恰好调度一次 FILE_CHANGE_UPDATED 回环
    assert loop.call_soon_threadsafe.call_count == 2


def test_file_change_updated_aligns_stats_by_order() -> None:
    """``_publish_file_change_updated`` 的增删统计按顺序对齐。

    回归缺陷：该方法此前用「过滤非 dict 后」列表的 index 建统计索引，却用
    「原始」changes 的 index 取值；一旦 changes 混入非 dict 元素，排在它
    之后的文件增删行数会错配到别的文件或直接归零，前端 diff 实时展示失真。
    本用例构造混入 ``"junk"`` 的 changes，验证 a.py / b.py 的增删统计各自正确。

    可能发现的缺陷：统计错配 / 归零导致 FILE_CHANGE_UPDATED 的 additions、
    deletions 与 path 不对应。
    """
    tool_definitions = [_definition("file_tool", parallel=False)]
    calls = [_make_call("f1", "file_tool")]

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        return ToolObservation(
            tool_name=call.tool_name,
            status="success",
            content="ok",
            error="",
            reason="",
            retryable=False,
            tool_call_id=call.call_id,
            data={
                "changes": [
                    "junk",
                    {"path": "a.py", "status": "modified", "before": "x\n", "after": "x\ny\n"},
                    {"path": "b.py", "status": "modified", "before": "x\ny\n", "after": "x\n"},
                ]
            },
        )

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    bus = MagicMock(spec=RuntimeEventBus)

    def _deliver(callback: Callable[..., None], *args: object) -> None:
        callback(*args)  # 真正投递，使 bus.publish 可断言

    loop = MagicMock()
    loop.call_soon_threadsafe.side_effect = _deliver
    service = _make_service(
        scheduler,
        should_cancel=lambda: False,
        tool_definitions=tool_definitions,
        event_bus=bus,
    )

    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        execution_context=_context("t1", "turn1"),
        write_event=MagicMock(),
        running_loop=loop,
    )

    _assert_pairing_closed(calls, result)
    published = {
        e.payload.path: cast(FileChangeUpdatedPayload, e.payload)
        for e in (args.args[0] for args in bus.publish.call_args_list)
        if e.event_type == EventType.FILE_CHANGE_UPDATED
    }
    assert published["a.py"].additions == 1
    assert published["a.py"].deletions == 0
    assert published["b.py"].additions == 0
    assert published["b.py"].deletions == 1


def test_file_change_updated_duplicate_path_stats_are_isolated() -> None:
    """同一 path 多次变更时，每次变更取自己对应的增删统计。

    回归缺陷：统计若按 path 字典对齐，同 path 多条时后一条覆盖前一条，
    前面的变更会错配到最后一条的增删行数（或归零）。本用例让 a.py 出现
    两次变更（先 +1 行、再 +1 行），断言两次 FILE_CHANGE_UPDATED 各自
    携带本次变更的 additions，而不是共享最后一条。

    可能发现的缺陷：duplicate path 下 additions/deletions 错配或缺失。
    """
    tool_definitions = [_definition("file_tool", parallel=False)]
    calls = [_make_call("f1", "file_tool")]

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        return ToolObservation(
            tool_name=call.tool_name,
            status="success",
            content="ok",
            error="",
            reason="",
            retryable=False,
            tool_call_id=call.call_id,
            data={
                "changes": [
                    {"path": "a.py", "status": "modified", "before": "x\n", "after": "x\ny\n"},
                    {"path": "a.py", "status": "modified", "before": "x\n", "after": "x\ny\nz\n"},
                ]
            },
        )

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    bus = MagicMock(spec=RuntimeEventBus)

    def _deliver(callback: Callable[..., None], *args: object) -> None:
        callback(*args)

    loop = MagicMock()
    loop.call_soon_threadsafe.side_effect = _deliver
    service = _make_service(
        scheduler,
        should_cancel=lambda: False,
        tool_definitions=tool_definitions,
        event_bus=bus,
    )

    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        execution_context=_context("t1", "turn1"),
        write_event=MagicMock(),
        running_loop=loop,
    )

    _assert_pairing_closed(calls, result)
    published = [
        cast(FileChangeUpdatedPayload, e.payload)
        for e in (args.args[0] for args in bus.publish.call_args_list)
        if e.event_type == EventType.FILE_CHANGE_UPDATED
    ]
    # 两次变更顺序发布，各自携带本次的 additions（顺序对齐，非共享最后一条）
    assert [p.additions for p in published] == [1, 2]
