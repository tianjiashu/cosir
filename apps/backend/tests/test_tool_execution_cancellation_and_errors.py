"""验证工具批次执行的失败收口：取消跳过与执行链内部 bug 均须补占位观察。

设计目标：``ToolExecutionService.run_calls_with_events`` 是对 ``AIMessage.tool_calls``
配对闭合的硬不变量收口点。无论失败来自「协作式取消」还是「执行链自身 bug（非工具
语义失败）」，都必须为每个未真正产出观察的 call 补一个占位 ``ToolObservation`` 并序列化为
``role="tool"`` 消息，使模型感知语义、协议不崩。其中「协作式取消」补 ``status="cancelled"``
的占位（根因是用户主动中断，与 ``status="error"`` 的真实执行故障语义不同）；「执行链 bug」
补 ``status="error"`` 的占位。占位的 ``display_data`` 必须在序列化前清空，模型只看到
``content``/``error``/``reason``。
"""

import json
import logging
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from app.models import RuntimeMessage
from app.models.enums.error_kind import ErrorKind
from app.models.enums.event_type import EventType
from app.models.payload.tool_call_finished_payload import ToolCallFinishedPayload
from app.service.tool_execution.run_result import ToolRunResult
from app.service.tool_execution.tool_execution_service import ToolExecutionService
from app.tools.schemas import ToolCall, ToolDefinition, ToolObservation
from app.tools.tool_execute.tool_error import tool_cancelled, tool_error
from app.tools.tool_execute.tool_scheduler import ToolScheduler
from app.tools.tool_registry import ToolRegistry


def _make_call(call_id: str, tool_name: str = "read_file") -> ToolCall:
    """构造一个最小可用的模型工具调用。"""
    return ToolCall(
        call_id=call_id,
        tool_name=tool_name,
        arguments={"path": f"/workspace/{call_id}.txt"},
    )


def _success_observation(call: ToolCall) -> ToolObservation:
    """构造一个成功观察（与调度器正常返回同构）。"""
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
) -> ToolExecutionService:
    """用给定调度器、取消回调与工具定义装配服务（trace/event_bus 取默认空实现）。"""
    return ToolExecutionService(
        scheduler=scheduler,
        agent_id="test-agent",
        should_cancel=should_cancel,
        trace_recorder=MagicMock(),
        tool_definitions=tool_definitions,
    )


class _EmptyArgs(BaseModel):
    """并行调度测试用的最小参数模型（service 层不执行 handler）。"""


class _RequiredArgs(BaseModel):
    """需要必填字段的测试参数模型，用来触发 schema validation 失败。"""

    path: str


def _parallel_definition(name: str, parallel: bool) -> ToolDefinition:
    """构造仅声明调度模式的工具定义（handler 不参与 service 层执行）。"""
    return ToolDefinition(
        name=name,
        description=name,
        permission="tool_read",
        handler=lambda: None,
        args_model=_EmptyArgs,
        parallel_mode="parallel" if parallel else "serial",
    )


def _count_tool_messages(result: ToolRunResult) -> list[RuntimeMessage]:
    """返回结果中全部 tool 角色消息。"""
    return [m for m in result.messages_for_model if m.role == "tool"]


def _assert_all_calls_have_placeholder(calls: list[ToolCall], result: ToolRunResult) -> None:
    """断言每个 call 都有一条 tool 消息，且 metadata 的 tool_call_id 配对闭合。"""
    produced_ids = {
        m.metadata.get("tool_call_id") for m in result.messages_for_model if m.role == "tool"
    }
    expected_ids = {c.call_id for c in calls}
    assert (
        produced_ids == expected_ids
    ), f"占位不闭合：produced={produced_ids}, expected={expected_ids}"


def test_cancellation_before_first_call_fills_placeholders_for_all() -> None:
    """取消信号在执行前即生效：所有 call 都应补取消占位，且均 status=cancelled。"""
    calls = [_make_call("c1"), _make_call("c2"), _make_call("c3")]
    scheduler = MagicMock(spec=ToolScheduler)
    service = _make_service(scheduler, should_cancel=lambda: True)

    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=MagicMock(),
    )

    # 调度器从未被调用（全部跳过）
    scheduler.execute.assert_not_called()
    _assert_all_calls_have_placeholder(calls, result)
    assert all(o.status == "cancelled" for o in result.observations)
    assert all(o.retryable is False for o in result.observations)
    assert len(result.observations) == len(calls)


def test_tool_cancelled_factory_produces_cancelled_terminal_observation() -> None:
    """tool_cancelled 工厂产出规范的取消态观察：确定性终态、不可重试、error_kind=cancelled。"""
    observation = tool_cancelled(
        tool_name="delegate_task",
        reason="the parent turn was cancelled; do not retry identical arguments.",
        error="delegate_task child cancelled: parent turn cancelled",
        tool_call_id="call-1",
    )

    assert observation.status == "cancelled"
    assert observation.tool_name == "delegate_task"
    assert observation.retryable is False
    assert observation.error == "delegate_task child cancelled: parent turn cancelled"
    assert observation.data is not None
    assert observation.data["error_kind"] == "cancelled"


def test_cancellation_mid_batch_fills_placeholders_for_remaining() -> None:
    """执行首个 call 成功后取消：已执行的保留成功，剩余补取消占位。"""
    calls = [_make_call("c1"), _make_call("c2"), _make_call("c3")]

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        return _success_observation(call)

    # 第一个 call 执行完后取消生效
    state = {"ran": False}

    def _should_cancel() -> bool:
        if state["ran"]:
            return True
        state["ran"] = True
        return False

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    service = _make_service(scheduler, should_cancel=_should_cancel)

    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=MagicMock(),
    )

    statuses = [o.status for o in result.observations]
    # 第一个成功，其余两个为取消占位
    assert statuses[0] == "success"
    assert statuses[1:] == ["cancelled", "cancelled"]
    _assert_all_calls_have_placeholder(calls, result)
    assert len(result.observations) == len(calls)


def test_internal_execution_bug_fills_error_placeholder_for_failed_call() -> None:
    """执行链自身抛异常（非工具语义失败）：当前 call 收口为 error 占位并写日志。"""
    calls = [_make_call("c1"), _make_call("c2")]

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        if call.call_id == "c1":
            raise RuntimeError("bug in scheduler internal logic")
        return _success_observation(call)

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    service = _make_service(scheduler, should_cancel=lambda: False)
    events: list[tuple[object, object]] = []

    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=lambda event_type, payload: events.append((event_type, payload)),
    )

    # 两个 call 都有占位（不悬空、不崩协议）
    _assert_all_calls_have_placeholder(calls, result)
    # c1 是内部错误占位，c2 正常成功
    by_id = {o.tool_call_id: o for o in result.observations}
    assert by_id["c1"].status == "error"
    assert "internal execution error" in by_id["c1"].error
    finished_by_id = {payload.tool_call_id: payload for _event, payload in events}
    assert finished_by_id["c1"].data["error_kind"] == "runtime_failed"
    assert by_id["c2"].status == "success"
    # 内部错误不得标记为可重试（确定性 runtime 失败）
    assert by_id["c1"].retryable is False


def test_scheduler_unknown_tool_sets_error_kind() -> None:
    """工具名未注册时，错误分类应标记为 unknown_tool。"""
    scheduler = ToolScheduler(ToolRegistry())
    call = ToolCall(call_id="missing", tool_name="missing_tool", arguments={})

    observation = scheduler.execute(call, execution_context=MagicMock())

    assert observation.status == "error"
    assert observation.data is not None
    assert observation.data["error_kind"] == "unknown_tool"


def test_scheduler_schema_validation_sets_error_kind() -> None:
    """工具参数不符合 schema 时，错误分类应标记为 schema_invalid。"""
    definition = ToolDefinition(
        name="needs_path",
        description="needs path",
        permission="read",
        handler=lambda **_kwargs: None,
        args_model=_RequiredArgs,
    )
    scheduler = ToolScheduler(ToolRegistry([definition]))
    call = ToolCall(call_id="bad_args", tool_name="needs_path", arguments={})

    observation = scheduler.execute(call, execution_context=MagicMock())

    assert observation.status == "error"
    assert observation.data is not None
    assert observation.data["error_kind"] == "schema_invalid"


def test_tool_error_kind_cannot_be_overridden_by_display_data() -> None:
    """展示数据不能覆盖稳定错误分类字段。"""
    observation = tool_error(
        tool_name="read_file",
        error="failed",
        reason="failed",
        display_data={"error_kind": "unknown_tool", "detail": "kept"},
        error_kind=ErrorKind.RUNTIME_FAILED,
    )

    assert observation.data is not None
    assert observation.data["error_kind"] == "runtime_failed"
    assert observation.data["detail"] == "kept"


def test_no_cancellation_no_bug_passthrough_success() -> None:
    """正常批次：观察与消息一一对应，无多余占位。"""
    calls = [_make_call("c1"), _make_call("c2")]

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        return _success_observation(call)

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    service = _make_service(scheduler, should_cancel=lambda: False)

    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=MagicMock(),
    )

    _assert_all_calls_have_placeholder(calls, result)
    assert all(o.status == "success" for o in result.observations)
    assert len(result.observations) == len(calls)


def test_cancelled_placeholder_does_not_leak_display_data_to_model() -> None:
    """取消占位的模型消息必须清空 display_data，不能把 data 字典序列化进 content_text。"""
    calls = [_make_call("c1"), _make_call("c2")]
    scheduler = MagicMock(spec=ToolScheduler)
    service = _make_service(scheduler, should_cancel=lambda: True)

    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=MagicMock(),
    )

    for msg in _count_tool_messages(result):
        payload = json.loads(msg.content_text)
        # tool_error 会把整段观察（除 content）写进 observation.data，若未清空会泄漏
        assert "data" not in payload, f"占位模型消息泄漏了 display_data: {payload}"


def test_internal_execution_bug_writes_error_log_with_stack(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """执行链 bug 必须写 error 日志（事件名 + 中文 msg + 上下文 data + 堆栈），可排查。"""
    calls = [_make_call("c1")]

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        raise RuntimeError("bug in scheduler internal logic")

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    service = _make_service(scheduler, should_cancel=lambda: False)

    caplog.set_level(logging.ERROR)
    service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=MagicMock(),
    )
    records = [r for r in caplog.records if r.getMessage() == "tool_call_internal_error"]
    assert records, "未记录 tool_call_internal_error 日志"
    record = records[0]
    assert record.exc_info is not None, "error 日志缺少堆栈，无法定位根因"
    record_data = getattr(record, "data", {})
    assert "tool_name" in record_data and "tool_call_id" in record_data


def test_cancellation_writes_warning_log_with_skipped_ids(caplog: pytest.LogCaptureFixture) -> None:
    """整批因取消跳过时须写 warning 日志，记录 skipped_call_ids 供复盘。"""
    calls = [_make_call("c1"), _make_call("c2")]
    scheduler = MagicMock(spec=ToolScheduler)
    service = _make_service(scheduler, should_cancel=lambda: True)

    caplog.set_level(logging.WARNING)
    service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=MagicMock(),
    )
    records = [r for r in caplog.records if r.getMessage() == "tool_calls_cancelled_not_executed"]
    assert records, "取消跳过未记 warning 日志"
    record_data = getattr(records[0], "data", {})
    assert record_data["total"] == 2
    assert record_data["executed"] == 0
    assert set(record_data["skipped_call_ids"]) == {"c1", "c2"}


def test_cancel_placeholder_reason_is_deterministic_non_retryable() -> None:
    """取消占位的 reason 必须告诉模型这是确定性终态、不必重试。"""
    calls = [_make_call("c1")]
    scheduler = MagicMock(spec=ToolScheduler)
    service = _make_service(scheduler, should_cancel=lambda: True)

    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=MagicMock(),
    )

    obs = result.observations[0]
    assert obs.status == "cancelled"
    assert obs.retryable is False
    assert "cancelled" in obs.reason
    assert "do not retry" in obs.reason


def test_missing_write_event_raises_runtime_error() -> None:
    """write_event 为 None 时直接抛 RuntimeError（调用方必须提供事件回调）。"""
    calls = [_make_call("c1")]
    scheduler = MagicMock(spec=ToolScheduler)
    service = _make_service(scheduler, should_cancel=lambda: False)

    with pytest.raises(RuntimeError, match="write_event is None"):
        service.run_calls_with_events(
            step_id="s1",
            calls=calls,
            write_event=None,
        )


def test_duplicate_call_id_still_closes_pairing_when_second_skipped() -> None:
    """同批两个 call 共享同一 call_id 时，取消第二个也必须补占位、配对闭合。

    修复前用 ``executed_call_ids``（set[str]）判重，同 id 的第二个 call 会被
    误判为「已执行」而不补占位；修复后按原始索引判重，重复 call_id 不影响配对。
    """
    calls = [_make_call("dup"), _make_call("dup")]

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        return _success_observation(call)

    # 第一个 call 执行完后取消生效（与 test_cancellation_mid_batch 同模式）
    state = {"ran": False}

    def _should_cancel() -> bool:
        if state["ran"]:
            return True
        state["ran"] = True
        return False

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    service = _make_service(scheduler, should_cancel=_should_cancel)

    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=MagicMock(),
    )

    # 配对闭合不变量：观察与消息必须与 calls 一一对应
    assert len(result.observations) == len(
        calls
    ), f"配对不闭合：observations={len(result.observations)}, calls={len(calls)}"
    _assert_all_calls_have_placeholder(calls, result)
    assert [o.status for o in result.observations] == ["success", "cancelled"]


def test_write_event_exception_is_contained_without_breaking_execution() -> None:
    """write_event 抛异常（如 SSE 断连）不得向上穿透，也不得打断工具执行。

    修复前 TOOL_CALL_STARTED / TOOL_CALL_FINISHED 事件写入在 try 块之外，
    异常直接穿透 run_calls_with_events；修复后由 ``_emit_event_safely`` 收口：
    事件通道故障只记 error 日志，工具照常执行、观察照常产出、配对闭合。
    """
    calls = [_make_call("c1"), _make_call("c2")]

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        return _success_observation(call)

    def _exploding_write_event(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("event bus unavailable")

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    service = _make_service(scheduler, should_cancel=lambda: False)

    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=_exploding_write_event,
    )

    # 异常被收口：方法不抛出、工具正常执行、观察与消息配对闭合
    assert len(result.observations) == len(
        calls
    ), f"配对不闭合：observations={len(result.observations)}, calls={len(calls)}"
    _assert_all_calls_have_placeholder(calls, result)
    assert all(
        o.status == "success" for o in result.observations
    ), "事件通道故障不应把正常执行的工具观察降级为 error"


def test_mixed_parallel_and_serial_executes_all_in_original_order() -> None:
    """混合批次（并行+串行交错）全部执行，返回顺序与原始 calls 一致。

    ``run_calls_with_events`` 入口按调度模式分流：串行调用先逐个执行，随后并行
    组统一并发执行。返回的观察必须按原始 index 排序，且消息与 calls 一一配对闭合。
    """
    tool_definitions = [
        _parallel_definition("parallel_tool", parallel=True),
        _parallel_definition("serial_tool", parallel=False),
    ]
    calls = [
        _make_call("p1", "parallel_tool"),
        _make_call("s1", "serial_tool"),
        _make_call("p2", "parallel_tool"),
        _make_call("p3", "parallel_tool"),
    ]

    execution_order: list[str] = []

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        execution_order.append(call.call_id)
        return _success_observation(call)

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    service = _make_service(
        scheduler,
        should_cancel=lambda: False,
        tool_definitions=tool_definitions,
    )

    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=MagicMock(),
    )

    assert [o.tool_call_id for o in result.observations] == ["p1", "s1", "p2", "p3"]
    assert all(o.status == "success" for o in result.observations)
    _assert_all_calls_have_placeholder(calls, result)
    # 分流执行顺序：串行组（s1）先执行，随后并行组（p1/p2/p3）并发；并行组内顺序不定
    assert execution_order.index("s1") < execution_order.index("p1")
    assert execution_order.index("s1") < execution_order.index("p2")
    assert execution_order.index("s1") < execution_order.index("p3")


def test_mixed_parallel_serial_cancel_after_serial_keeps_pairing() -> None:
    """混合批次中串行 call 执行后取消：并行组整体跳过补占位、配对闭合。

    串行组（s1）先执行，执行后取消生效；并行组（p1/p2）因取消整体跳过，
    全部补 error 占位，观察按原始 index 排序返回。
    """
    calls = [
        _make_call("p1", "parallel_tool"),
        _make_call("s1", "serial_tool"),
        _make_call("p2", "parallel_tool"),
    ]
    tool_definitions = [
        _parallel_definition("parallel_tool", parallel=True),
        _parallel_definition("serial_tool", parallel=False),
    ]

    executed: list[str] = []

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        executed.append(call.call_id)
        return _success_observation(call)

    # 串行 call 执行后取消生效（语义化条件，不依赖内部取消检查次数）
    def _should_cancel() -> bool:
        return "s1" in executed

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    service = _make_service(
        scheduler,
        should_cancel=_should_cancel,
        tool_definitions=tool_definitions,
    )

    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=MagicMock(),
    )

    assert [o.tool_call_id for o in result.observations] == ["p1", "s1", "p2"]
    assert [o.status for o in result.observations] == ["cancelled", "success", "cancelled"]
    assert executed == ["s1"]
    _assert_all_calls_have_placeholder(calls, result)


def test_mixed_parallel_serial_duplicate_call_id_keeps_pairing() -> None:
    """混合批次中重复 call_id：取消后仍按索引补占位、配对闭合。

    串行组（s1）执行后取消，并行组（两个 dup）整体跳过补占位；重复 call_id
    不导致第二个被误判「已执行」，三个观察按原始 index 返回。
    """
    calls = [
        _make_call("dup", "parallel_tool"),
        _make_call("s1", "serial_tool"),
        _make_call("dup", "parallel_tool"),
    ]
    tool_definitions = [
        _parallel_definition("parallel_tool", parallel=True),
        _parallel_definition("serial_tool", parallel=False),
    ]

    executed: list[str] = []

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        executed.append(call.call_id)
        return _success_observation(call)

    def _should_cancel() -> bool:
        return "s1" in executed

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    service = _make_service(
        scheduler,
        should_cancel=_should_cancel,
        tool_definitions=tool_definitions,
    )

    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=MagicMock(),
    )

    # 三个 call 都有对应观察（重复 id 不导致第二个被误判「已执行」）
    assert len(result.observations) == len(
        calls
    ), f"配对不闭合：observations={len(result.observations)}, calls={len(calls)}"
    assert [o.status for o in result.observations] == ["cancelled", "success", "cancelled"]


def test_mixed_parallel_serial_write_event_exception_is_contained() -> None:
    """混合批次中 write_event 抛异常：不穿透、工具照常执行、配对闭合。"""
    calls = [
        _make_call("p1", "parallel_tool"),
        _make_call("s1", "serial_tool"),
        _make_call("p2", "parallel_tool"),
    ]
    tool_definitions = [
        _parallel_definition("parallel_tool", parallel=True),
        _parallel_definition("serial_tool", parallel=False),
    ]

    def _scheduler_execute(call: ToolCall, **_kwargs: object) -> ToolObservation:
        return _success_observation(call)

    def _exploding_write_event(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("event bus unavailable")

    scheduler = MagicMock(spec=ToolScheduler)
    scheduler.execute.side_effect = _scheduler_execute
    service = _make_service(
        scheduler,
        should_cancel=lambda: False,
        tool_definitions=tool_definitions,
    )

    result = service.run_calls_with_events(
        step_id="s1",
        calls=calls,
        write_event=_exploding_write_event,
    )

    assert len(result.observations) == len(
        calls
    ), f"配对不闭合：observations={len(result.observations)}, calls={len(calls)}"
    _assert_all_calls_have_placeholder(calls, result)
    assert all(o.status == "success" for o in result.observations)


def test_cancelled_observation_emit_status_kept_as_cancelled_not_collapsed_to_error() -> None:
    """经过执行的取消观察发出 TOOL_CALL_FINISHED 事件时，payload.status 须透传 cancelled。

    回归防护：``_handle_completed_observation`` 历史上把非 success 一律塌缩成 error，
    会让前端取消态仍显示「失败」、与本次修复目标矛盾。此测试固化「取消不塌缩」契约——
    注意取消占位（call 边界前跳过）不 emit 事件只落库配对，故此处直接驱动已执行路径。
    """
    from app.tools.tool_execute.tool_error import tool_cancelled

    scheduler = MagicMock(spec=ToolScheduler)
    service = _make_service(scheduler)
    events: list[tuple[object, object]] = []
    cancelled = tool_cancelled(
        tool_name="delegate_task",
        reason="the delegated child agent was cancelled; do not retry identical arguments.",
        error="delegate_task child cancelled: parent turn cancelled",
        tool_call_id="c1",
    )
    service._handle_completed_observation(
        "s1",
        cancelled,
        execution_context=None,
        write_event=lambda event_type, payload: events.append((event_type, payload)),
        loop=None,
    )

    finished = [
        payload
        for event_type, payload in events
        if event_type == EventType.TOOL_CALL_FINISHED
    ]
    assert len(finished) == 1
    payload = finished[0]
    assert isinstance(payload, ToolCallFinishedPayload)
    assert payload.tool_call_id == "c1"
    assert payload.status == "cancelled"
    assert payload.retryable is False
    assert "cancelled" in (payload.reason or "")

    # 对照：error 观察仍透传 error，不被本次改动波及。
    from app.tools.tool_execute.tool_error import tool_error

    events.clear()
    failed = tool_error(
        tool_name="read_file",
        error="permission denied",
        reason="access denied by workspace boundary.",
        tool_call_id="c2",
    )
    service._handle_completed_observation(
        "s1",
        failed,
        execution_context=None,
        write_event=lambda event_type, payload: events.append((event_type, payload)),
        loop=None,
    )
    finished_err = [
        payload
        for event_type, payload in events
        if event_type == EventType.TOOL_CALL_FINISHED
    ]
    assert finished_err[0].status == "error"
