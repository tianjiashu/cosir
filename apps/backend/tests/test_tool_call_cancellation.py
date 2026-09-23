"""工具级取消（只中止一次工具调用）的行为契约测试。

覆盖三层：

1. ``ToolCallCancellationRegistry``：键规范（run + tool_call 二元组）、跨 run 隔离、
   清理与并发不串扰；
2. ``ToolHandlerRunner``：工具级信号命中即中止本次调用、不误伤同 run 的兄弟调用、
   不写 run 级注册表、不投影 Transport 终态、信号释放；
3. ``ConversationRunExecutor.cancel_tool_call`` 与 HTTP 端点的状态码映射。

终态投影的时序属于「执行出口」契约，本文件只断言它**不发生在执行层**：终态必须晚于
``ToolHandlerRunner`` 的强杀与输出排空（见 ``ToolExecutor`` 的出口投影与
``tool_terminal_projection``）。投影内容本身见 ``tests/test_tool_terminal_projection.py``。

本文件只验证「工具级取消」这一新增能力，run 级取消的既有契约见
``tests/test_tool_handler_runner_cancellation.py``。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import BaseModel

from app.assistant_transport.event.tool_call_event import ToolCallStatusChangedEvent
from app.core.runtime.conversation_run_executor import ConversationRunExecutor
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.runtime.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.tools.schemas import ToolCall, ToolDefinition, ToolExecutionContext
from app.core.tools.tool_execute import tool_handler_runner
from app.core.tools.tool_execute.tool_cancelled import tool_cancelled
from app.core.tools.tool_execute.tool_executor import ToolExecutor
from app.core.tools.tool_execute.tool_handler_runner import ToolHandlerRunner
from app.core.tools.tool_registry import ToolRegistry

# 本文件使用的 run / 调用标识：集中登记，供自动清理夹具逐项复位。
_RUN_A = 950001
_RUN_B = 950002
_RUN_EXECUTOR = 950003
_CLEANUP_RUN_IDS = (_RUN_A, _RUN_B, _RUN_EXECUTOR)


class _NoArgs(BaseModel):
    """空参数模型：本文件的测试工具都不需要结构化入参。"""


# ---------------------------------------------------------------------------
# 模块级 handler（spawn 子进程要求可 pickle）
# ---------------------------------------------------------------------------


def _handler_sleeps_then_writes(
    marker_path: str,
    sleep_seconds: float,
    execution_context: object = None,
    **_kwargs: object,
) -> str:
    """睡满指定秒数后写标记文件并返回文本；用于证明调用是否跑到终点。"""

    time.sleep(sleep_seconds)
    Path(marker_path).write_text("finished", encoding="utf-8")
    return "completed"


def _handler_marks_own_call_signal(
    run_id: int,
    call_id: str,
    execution_context: object = None,
    **_kwargs: object,
) -> str:
    """执行途中给自己标记工具级取消信号后正常返回；用于覆盖 thread 模式执行后边界。"""

    tool_call_cancellation_registry.mark_cancelled(run_id, call_id)
    return "handler-completed"


# ---------------------------------------------------------------------------
# 工厂与替身
# ---------------------------------------------------------------------------


def _make_tool(
    handler: Any,
    *,
    name: str = "probe_tool",
    execution_mode: str = "process",
    timeout_seconds: float = 30.0,
) -> ToolDefinition:
    """构造测试用 ToolDefinition。"""

    return ToolDefinition(
        name=name,
        description="probe tool",
        permission="test",
        handler=handler,
        args_model=_NoArgs,
        timeout_seconds=timeout_seconds,
        execution_mode=execution_mode,
    )


def _make_context(
    run_id: int,
    *,
    tool_call_id: str = "",
) -> ToolExecutionContext:
    """构造绑定了 run 与工具调用身份的执行上下文。"""

    return ToolExecutionContext(
        task_id=11,
        workspace_id=22,
        workspace_root=Path("."),
        run_id=run_id,
        tool_call_id=tool_call_id,
    )


class _RunService:
    """只实现执行器读取 run 所需的最小语义。"""

    def __init__(self, run_id: int = _RUN_EXECUTOR) -> None:
        """构造一条可读取的 running run。"""

        self.run = SimpleNamespace(id=run_id, task_id=7, status="running")

    def get_run(self, _run_id: int) -> SimpleNamespace:
        """返回预置 run 记录。"""

        return self.run


def _build_executor(run_service: object) -> ConversationRunExecutor:
    """绕过依赖装配构造只注入 run service 与进程内信号源的执行器实例。"""

    executor = ConversationRunExecutor.__new__(ConversationRunExecutor)
    executor._run_service = run_service
    executor._signal = cancellation_registry
    executor._event_projector = None
    executor._executions = {}
    return executor


@pytest.fixture(autouse=True)
def _isolated_cancellation_registries():
    """每个用例前后清空进程内取消信号，避免用例间串扰。"""

    for run_id in _CLEANUP_RUN_IDS:
        cancellation_registry.clear(run_id)
        tool_call_cancellation_registry.clear_run(run_id)
    yield
    for run_id in _CLEANUP_RUN_IDS:
        cancellation_registry.clear(run_id)
        tool_call_cancellation_registry.clear_run(run_id)


# ---------------------------------------------------------------------------
# 1. 注册表本体
# ---------------------------------------------------------------------------


def test_registry_tracks_exact_run_and_call_keys() -> None:
    """信号必须精确到 (run_id, tool_call_id)：不同 run 或不同调用不得串扰。"""

    tool_call_cancellation_registry.mark_cancelled(_RUN_A, "call-1")

    assert tool_call_cancellation_registry.is_cancelled(_RUN_A, "call-1") is True
    assert tool_call_cancellation_registry.is_cancelled(_RUN_B, "call-1") is False
    assert tool_call_cancellation_registry.is_cancelled(_RUN_A, "call-2") is False
    assert tool_call_cancellation_registry.is_cancelled(_RUN_A, "call-1 ") is False


def test_registry_ignores_empty_tool_call_id() -> None:
    """空 tool_call_id 不构成可取消范围：标记与查询都按未取消处理。"""

    tool_call_cancellation_registry.mark_cancelled(_RUN_A, "")

    assert tool_call_cancellation_registry.is_cancelled(_RUN_A, "") is False
    assert tool_call_cancellation_registry.is_cancelled(_RUN_A, "call-1") is False


def test_registry_normalizes_numeric_string_run_id_both_ways() -> None:
    """数字字符串 run_id 与整数 run_id 必须命中同一个键（读写双向归一）。"""

    tool_call_cancellation_registry.mark_cancelled(str(_RUN_A), "call-1")

    assert tool_call_cancellation_registry.is_cancelled(_RUN_A, "call-1") is True

    tool_call_cancellation_registry.clear(str(_RUN_A), "call-1")

    assert tool_call_cancellation_registry.is_cancelled(_RUN_A, "call-1") is False


def test_registry_clear_run_only_removes_target_run() -> None:
    """clear_run 只清理目标 run 的全部信号，不影响其它 run 的同名调用。"""

    tool_call_cancellation_registry.mark_cancelled(_RUN_A, "call-1")
    tool_call_cancellation_registry.mark_cancelled(_RUN_A, "call-2")
    tool_call_cancellation_registry.mark_cancelled(_RUN_B, "call-1")

    tool_call_cancellation_registry.clear_run(_RUN_A)

    assert tool_call_cancellation_registry.is_cancelled(_RUN_A, "call-1") is False
    assert tool_call_cancellation_registry.is_cancelled(_RUN_A, "call-2") is False
    assert tool_call_cancellation_registry.is_cancelled(_RUN_B, "call-1") is True


def test_registry_unrepresentable_run_id_never_matches_and_never_raises() -> None:
    """无法归一为整数的 run 标识既不命中也不抛异常（读取与清理都按不可命中处理）。"""

    tool_call_cancellation_registry.mark_cancelled("not-a-run", "call-1")
    tool_call_cancellation_registry.clear("not-a-run", "call-1")
    tool_call_cancellation_registry.clear_run("not-a-run")

    assert tool_call_cancellation_registry.is_cancelled("not-a-run", "call-1") is False
    # 不可命中的键不得影响合法整数键。
    tool_call_cancellation_registry.mark_cancelled(_RUN_A, "call-1")
    assert tool_call_cancellation_registry.is_cancelled(_RUN_A, "call-1") is True


def test_registry_concurrent_access_does_not_crosstalk() -> None:
    """并发读写不同键不得串扰：主线程预置的见证键对所有工作线程始终可见。"""

    rounds = 200
    errors: list[BaseException] = []
    witness_call_id = "call-witness"
    tool_call_cancellation_registry.mark_cancelled(_RUN_A, witness_call_id)

    def _worker(offset: int) -> None:
        try:
            for index in range(rounds):
                call_id = f"call-{offset}-{index}"
                tool_call_cancellation_registry.mark_cancelled(_RUN_A, call_id)
                assert tool_call_cancellation_registry.is_cancelled(_RUN_A, call_id) is True
                # 并发写入不得挤掉见证键，也不得让本线程误命中别的 run。
                assert tool_call_cancellation_registry.is_cancelled(_RUN_A, witness_call_id) is True
                assert tool_call_cancellation_registry.is_cancelled(_RUN_B, call_id) is False
                tool_call_cancellation_registry.clear(_RUN_A, call_id)
        except BaseException as exc:  # 线程异常需回传主线程断言，不能静默丢失
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(offset,)) for offset in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert tool_call_cancellation_registry.is_cancelled(_RUN_A, witness_call_id) is True
    assert tool_call_cancellation_registry.is_cancelled(_RUN_A, "call-0-0") is False


# ---------------------------------------------------------------------------
# 2. 工具执行层
# ---------------------------------------------------------------------------


def test_tool_call_signal_cancels_only_the_named_call_in_parallel(tmp_path: Path) -> None:
    """并行执行时只中止被点名的调用：兄弟调用必须跑到终点。

    这是本需求的核心不变量——工具级取消只打断工具执行，不打断 run 与同批其它调用。
    """

    cancelled_marker = tmp_path / "cancelled.txt"
    sibling_marker = tmp_path / "sibling.txt"
    tool = _make_tool(_handler_sleeps_then_writes, name="proc_parallel")
    observations: dict[str, Any] = {}

    def _execute(call_id: str, marker: Path, sleep_seconds: float) -> None:
        observations[call_id] = ToolHandlerRunner().execute(
            tool,
            {"marker_path": str(marker), "sleep_seconds": sleep_seconds},
            _make_context(_RUN_A),
            tool_call_id=call_id,
        )

    cancelled_thread = threading.Thread(
        target=_execute, args=("call-cancelled", cancelled_marker, 5.0)
    )
    sibling_thread = threading.Thread(target=_execute, args=("call-sibling", sibling_marker, 1.0))
    timer = threading.Timer(
        0.3, tool_call_cancellation_registry.mark_cancelled, args=(_RUN_A, "call-cancelled")
    )
    started = time.monotonic()
    try:
        cancelled_thread.start()
        sibling_thread.start()
        timer.start()
        cancelled_thread.join()
        cancelled_elapsed = time.monotonic() - started
        sibling_thread.join()
    finally:
        timer.cancel()

    assert observations["call-cancelled"].status == "cancelled"
    # 取消观察必须带面向模型的 reason（具体文案按取消来源决定，见 tool_cancelled）。
    assert observations["call-cancelled"].reason
    assert cancelled_elapsed < 2.5, f"被点名调用耗时 {cancelled_elapsed:.2f}s，疑似未被强杀"
    assert cancelled_marker.exists() is False

    assert observations["call-sibling"].status == "success"
    assert observations["call-sibling"].content == "completed"
    assert sibling_marker.exists() is True


def test_tool_call_signal_does_not_touch_run_level_registry() -> None:
    """工具级取消不得写入 run 级注册表，否则会把整个 agent 执行打断。"""

    tool = _make_tool(_handler_sleeps_then_writes, name="proc_run_isolation")
    tool_call_cancellation_registry.mark_cancelled(_RUN_A, "call-1")

    observation = ToolHandlerRunner().execute(
        tool,
        {"marker_path": "unused.txt", "sleep_seconds": 5.0},
        _make_context(_RUN_A),
        tool_call_id="call-1",
    )

    assert observation.status == "cancelled"
    assert cancellation_registry.is_cancelled(_RUN_A) is False


def _capture_terminal_projection(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """把共享投影收口的 projector 解析替换为记录事件的替身。

    ``ToolHandlerRunner`` 已不再自行投影，本替身用于断言「取消路径不会触达共享投影收口」：
    若有人把执行期投影重新接回执行层，这个列表就会非空。
    """

    captured: list[Any] = []

    class _ProjectorSpy:
        def process(self, event: object) -> None:
            captured.append(event)

    monkeypatch.setattr(
        "app.core.tools.tool_execute.tool_terminal_projection.get_conversation_event_projector",
        lambda: _ProjectorSpy(),
    )
    return captured


def test_cancelled_execution_returns_cancelled_observation() -> None:
    """工具级取消命中时返回取消观察，且不向上抛异常。"""

    tool_call_cancellation_registry.mark_cancelled(_RUN_A, "call-1")
    tool = _make_tool(_handler_sleeps_then_writes, name="proc_cancelled", execution_mode="thread")

    observation = ToolHandlerRunner().execute(
        tool,
        {"marker_path": "unused.txt", "sleep_seconds": 0.0},
        _make_context(_RUN_A),
        tool_call_id="call-1",
    )

    assert observation.status == "cancelled"
    assert observation.reason


def test_thread_mode_post_execution_boundary_converts_success_to_cancelled() -> None:
    """thread 模式执行后边界：handler 已成功返回，但执行期间出现取消信号则转取消观察。"""

    tool = _make_tool(
        _handler_marks_own_call_signal, name="thread_post_boundary", execution_mode="thread"
    )

    observation = ToolHandlerRunner().execute(
        tool,
        {"run_id": _RUN_A, "call_id": "call-post"},
        _make_context(_RUN_A),
        tool_call_id="call-post",
    )

    # 成功结果被丢弃并转为取消观察。
    assert observation.status == "cancelled"
    assert observation.content is None


def test_runner_cancellation_does_not_project_transport_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消只产出观察与日志：执行层不得投影 Transport 终态。

    终态必须晚于强杀与输出排空（见 ``_cancelled_observation`` 的时序说明），因此取消路径在
    整个执行期内不得触达共享投影收口。第二层护栏断言旧的通知旁路本身已不存在，避免它被重新
    引入而绕开该时序规则。
    """

    captured = _capture_terminal_projection(monkeypatch)
    tool_call_cancellation_registry.mark_cancelled(_RUN_A, "call-1")
    tool = _make_tool(_handler_sleeps_then_writes, name="proc_no_bypass", execution_mode="thread")

    observation = ToolHandlerRunner().execute(
        tool,
        {"marker_path": "unused.txt", "sleep_seconds": 0.0},
        _make_context(_RUN_A),
        tool_call_id="call-1",
    )

    assert observation.status == "cancelled"
    assert observation.reason
    assert captured == []
    assert not hasattr(tool_handler_runner, "_notify_tool_cancelled")
    assert not hasattr(tool_handler_runner, "get_conversation_event_projector")


def test_terminal_projection_runs_only_after_pipeline_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终态投影必须晚于执行管线收尾：进程强杀与输出排空完成前不得变更前端状态。

    ``ToolExecutor._execute_inner`` 只有在强杀（``_force_kill`` 的 terminate / kill 两轮
    join）与输出排空（``_finish_process_output_channel``）都做完之后才返回，因此「投影晚于它
    返回」等价于「投影晚于进程收尾」——这是取消语义的核心不变量。
    """

    order: list[str] = []
    cancelled = tool_cancelled("probe_tool", permission="test", tool_call_id="call-1")

    def _fake_inner(
        _self: ToolExecutor,
        _call: ToolCall,
        execution_context: ToolExecutionContext | None = None,
        allowed_tool_names: Any = None,
    ) -> Any:
        order.append("execute_inner")
        return cancelled

    monkeypatch.setattr(ToolExecutor, "_execute_inner", _fake_inner)
    monkeypatch.setattr(
        "app.core.tools.tool_execute.tool_executor.project_tool_terminal_state",
        lambda **_kwargs: order.append("project"),
    )
    executor = ToolExecutor(
        registry=ToolRegistry([_make_tool(_handler_sleeps_then_writes, execution_mode="thread")])
    )

    observation = executor.execute(
        ToolCall(tool_name="probe_tool", arguments={}, call_id="call-1"),
        execution_context=_make_context(_RUN_A),
    )

    assert observation is cancelled
    assert order == ["execute_inner", "project"]


def test_tool_call_signal_is_released_after_execution() -> None:
    """单次执行结束后（任意结果）工具级信号必须被释放，run 级信号不受影响。"""

    tool_call_cancellation_registry.mark_cancelled(_RUN_A, "call-1")
    cancellation_registry.mark_cancelled(_RUN_A)
    tool = _make_tool(_handler_sleeps_then_writes, name="proc_release", execution_mode="thread")

    observation = ToolHandlerRunner().execute(
        tool,
        {"marker_path": "unused.txt", "sleep_seconds": 0.0},
        _make_context(_RUN_A),
        tool_call_id="call-1",
    )

    assert observation.status == "cancelled"
    assert tool_call_cancellation_registry.is_cancelled(_RUN_A, "call-1") is False
    assert cancellation_registry.is_cancelled(_RUN_A) is True


def test_late_tool_call_signal_survives_until_run_cleanup() -> None:
    """点名了已结束调用的迟到信号不会被工具执行层消费，只能由 run 收尾兜底清理。"""

    tool_call_cancellation_registry.mark_cancelled(_RUN_A, "call-already-finished")

    assert tool_call_cancellation_registry.is_cancelled(_RUN_A, "call-already-finished") is True

    tool_call_cancellation_registry.clear_run(_RUN_A)

    assert tool_call_cancellation_registry.is_cancelled(_RUN_A, "call-already-finished") is False


def test_build_cancel_check_watches_both_signal_sources() -> None:
    """取消检查回调必须同时实时观察 run 级与工具级信号。"""

    context = _make_context(_RUN_A, tool_call_id="call-1")
    check = ToolHandlerRunner._build_cancel_check(context, "call-1")
    assert check is not None
    assert check() is False

    tool_call_cancellation_registry.mark_cancelled(_RUN_A, "call-1")
    assert check() is True
    tool_call_cancellation_registry.clear(_RUN_A, "call-1")
    assert check() is False

    cancellation_registry.mark_cancelled(_RUN_A)
    assert check() is True
    cancellation_registry.clear(_RUN_A)
    assert check() is False


# ---------------------------------------------------------------------------
# 3. 执行器与 HTTP 端点
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_tool_call_marks_signal_and_rejects_duplicate() -> None:
    """首次取消返回 True 并标记信号；同一调用重复取消返回 False；不触碰 run 级信号。"""

    executor = _build_executor(_RunService())

    assert await executor.cancel_tool_call(_RUN_EXECUTOR, "call-1") is True
    assert tool_call_cancellation_registry.is_cancelled(_RUN_EXECUTOR, "call-1") is True
    assert await executor.cancel_tool_call(_RUN_EXECUTOR, "call-1") is False
    assert cancellation_registry.is_cancelled(_RUN_EXECUTOR) is False


@pytest.mark.asyncio
async def test_cancel_tool_call_rolls_back_signal_when_run_missing() -> None:
    """run 不存在时撤销已标记的信号并抛 KeyError，API 据此映射 404。"""

    class _MissingRunService(_RunService):
        def get_run(self, _run_id: int) -> SimpleNamespace:
            raise KeyError(_run_id)

    executor = _build_executor(_MissingRunService())

    with pytest.raises(KeyError):
        await executor.cancel_tool_call(_RUN_EXECUTOR, "call-1")
    assert tool_call_cancellation_registry.is_cancelled(_RUN_EXECUTOR, "call-1") is False


@pytest.mark.asyncio
async def test_cancel_tool_call_endpoint_maps_status_codes() -> None:
    """端点把首次取消映射为 200、重复取消映射为 409、run 缺失映射为 404。"""

    from app.assistant_transport.assistant_api import cancel_tool_call

    executor = _build_executor(_RunService())

    response = await cancel_tool_call(_RUN_EXECUTOR, "call-1", executor)

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "run_id": _RUN_EXECUTOR,
        "tool_call_id": "call-1",
        "cancelled": True,
    }

    with pytest.raises(HTTPException) as duplicate:
        await cancel_tool_call(_RUN_EXECUTOR, "call-1", executor)
    assert duplicate.value.status_code == 409

    class _MissingRunService(_RunService):
        def get_run(self, _run_id: int) -> SimpleNamespace:
            raise KeyError(_run_id)

    missing_executor = _build_executor(_MissingRunService())
    with pytest.raises(HTTPException) as missing:
        await cancel_tool_call(_RUN_EXECUTOR, "call-2", missing_executor)
    assert missing.value.status_code == 404
    # 404 路径不得留下脏信号。
    assert tool_call_cancellation_registry.is_cancelled(_RUN_EXECUTOR, "call-2") is False


def test_cancel_tool_call_endpoint_is_registered() -> None:
    """工具级取消端点必须注册在真实 app 上，且与 run 级取消端点同级。"""

    from app.app import app

    routes = {
        (route.path, tuple(sorted(getattr(route, "methods", None) or ()))) for route in app.routes
    }

    assert ("/runs/{run_id}/tool-calls/{tool_call_id}/cancel", ("POST",)) in routes
    assert ("/runs/{run_id}/cancel", ("POST",)) in routes


# ---------------------------------------------------------------------------
# 4. 取消事件的 snapshot 投影口径
# ---------------------------------------------------------------------------


def _running_tool_state(tool_call_id: str = "call-9") -> dict[str, Any]:
    """构造只含一个 ``running`` tool-call part 的最小 snapshot，供投影纯函数断言。"""

    return {
        "current_run_id": 4,
        "runs": [
            {
                "runId": 4,
                "status": "running",
                "messages": [
                    {
                        "id": "m1",
                        "role": "assistant",
                        "parts": [
                            {
                                "type": "tool-call",
                                "toolCallId": tool_call_id,
                                "toolName": "execute_terminal",
                                "status": "running",
                                "args": {},
                                "isError": False,
                            }
                        ],
                    }
                ],
            }
        ],
    }


def test_cancelled_event_mutates_running_part_to_cancelled() -> None:
    """取消终态事件把 ``running`` part 迁移为 ``cancelled``：状态、提示与 ``isError`` 口径固定。

    谁在什么时机发出这条事件由执行出口决定（见上面的时序用例），本节只锁定事件本身的 snapshot
    投影口径，避免口径随取消链路改动而漂移。
    """

    event = ToolCallStatusChangedEvent(
        task_id=11,
        run_id=_RUN_A,
        tool_call_id="call-1",
        status="cancelled",
        error="已取消",
    )

    mutations = {
        mutation.path: mutation.value for mutation in event.plan(_running_tool_state("call-1"))
    }
    base = ("runs", 0, "messages", 0, "parts", 0)

    assert mutations[(*base, "status")] == "cancelled"
    assert mutations[(*base, "error")] == "已取消"
    assert mutations[(*base, "isError")] is False


def test_cancelled_observation_plan_tolerates_missing_tool_part() -> None:
    """目标 part 不存在时投影只降级为无 mutation，不得抛异常打断工具收尾。"""

    event = ToolCallStatusChangedEvent(
        task_id=11,
        run_id=_RUN_A,
        tool_call_id="call-absent",
        status="cancelled",
        error="已取消",
    )

    assert list(event.plan({"current_run_id": _RUN_A, "runs": []})) == []
