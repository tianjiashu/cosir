"""工具级取消（只中止一次工具调用）的行为契约测试。

覆盖三层：

1. ``ToolCallCancellationRegistry``：键规范（task + tool_call 二元组）、跨 Task 隔离、
   清理与并发不串扰；
2. ``ToolHandlerRunner``：工具级信号命中即中止本次调用、不误伤同批的兄弟调用、
   不写 Task 级注册表、不投影 Transport 终态、信号释放；

终态投影的时序属于「执行出口」契约，本文件只断言它**不发生在执行层**：终态必须晚于
``ToolHandlerRunner`` 的强杀与输出排空（见 ``tool_batch_runner._execute_call`` 在
``executor.execute`` 返回后经 ``terminal_event`` 发出的终态）。投影内容本身见
``tests/test_tool_terminal_projection.py``。

本文件只验证「工具级取消」这一新增能力，run 级取消的既有契约见
``tests/test_tool_handler_runner_cancellation.py``。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from app.assistant_transport.projection.event_projection import plan_event
from app.core.events import ToolCallStatusChangedEvent
from app.core.tool.schemas import ToolCall, ToolDefinition, ToolExecutionContext
from app.core.tool.execute import tool_handler_runner, tool_terminal_projection, tool_executor as tool_executor_module
from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.execute.tool_executor import ToolExecutor
from app.core.tool.execute.tool_handler_runner import ToolHandlerRunner
from app.core.tool.tool_registry import ToolRegistry
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.core.workflow.cancel.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.workflow.execution_state.tool_batch import ToolBatch, ValidToolCall
from app.core.workflow.node_helper import tool_batch_runner
from tests.runtime_context_stub import runtime_stub

# 同步用例没有运行中的事件循环；只有进程工具的实时输出与子 Run 调度会用到它。
_NO_LOOP: Any = None

# 执行上下文里的 run 标识；取消信号不按它记录，只用于构造上下文与投影事件。
_RUN_A = 950001
_RUN_EXECUTOR = 950003
# _make_context 绑定的 Task：Task 级与工具级取消信号都按它记录。
_TASK_ID = 11
# 另一个 Task：验证工具级信号不跨 Task 串扰。
_OTHER_TASK = 950002
_CLEANUP_TASK_IDS = (_TASK_ID, _OTHER_TASK)


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
    task_id: int,
    call_id: str,
    execution_context: object = None,
    **_kwargs: object,
) -> str:
    """执行途中给自己标记工具级取消信号后正常返回；用于覆盖 thread 模式执行后边界。"""

    tool_call_cancellation_registry.mark_cancelled(task_id, call_id)
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
        group="probe",
        description="probe tool",
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
        step_id="step-1",
        task_id=_TASK_ID,
        workspace_id=22,
        workspace_root=Path("."),
        run_id=run_id,
        tool_call_id=tool_call_id,
    )


@pytest.fixture(autouse=True)
def _isolated_cancellation_registries():
    """每个用例前后清空进程内取消信号，避免用例间串扰。"""

    cancellation_registry.consume(_TASK_ID)
    for task_id in _CLEANUP_TASK_IDS:
        tool_call_cancellation_registry.clear_task(task_id)
    yield
    cancellation_registry.consume(_TASK_ID)
    for task_id in _CLEANUP_TASK_IDS:
        tool_call_cancellation_registry.clear_task(task_id)


# ---------------------------------------------------------------------------
# 1. 注册表本体
# ---------------------------------------------------------------------------


def test_registry_tracks_exact_task_and_call_keys() -> None:
    """信号必须精确到 (task_id, tool_call_id)：不同 Task 或不同调用不得串扰。"""

    assert tool_call_cancellation_registry.mark_cancelled(_TASK_ID, "call-1") is True

    assert tool_call_cancellation_registry.is_cancelled(_TASK_ID, "call-1") is True
    assert tool_call_cancellation_registry.is_cancelled(_OTHER_TASK, "call-1") is False
    assert tool_call_cancellation_registry.is_cancelled(_TASK_ID, "call-2") is False
    assert tool_call_cancellation_registry.is_cancelled(_TASK_ID, "call-1 ") is False


def test_registry_mark_reports_duplicates() -> None:
    """重复标记幂等，并以返回值告知调用方「已标记过」。"""

    assert tool_call_cancellation_registry.mark_cancelled(_TASK_ID, "call-1") is True
    assert tool_call_cancellation_registry.mark_cancelled(_TASK_ID, "call-1") is False
    tool_call_cancellation_registry.clear(_TASK_ID, "call-1")
    assert tool_call_cancellation_registry.is_cancelled(_TASK_ID, "call-1") is False


def test_registry_clear_task_only_removes_target_task() -> None:
    """clear_task 只清理目标 Task 的全部信号，不影响其它 Task 的同名调用。"""

    tool_call_cancellation_registry.mark_cancelled(_TASK_ID, "call-1")
    tool_call_cancellation_registry.mark_cancelled(_TASK_ID, "call-2")
    tool_call_cancellation_registry.mark_cancelled(_OTHER_TASK, "call-1")

    tool_call_cancellation_registry.clear_task(_TASK_ID)

    assert tool_call_cancellation_registry.is_cancelled(_TASK_ID, "call-1") is False
    assert tool_call_cancellation_registry.is_cancelled(_TASK_ID, "call-2") is False
    assert tool_call_cancellation_registry.is_cancelled(_OTHER_TASK, "call-1") is True


def test_registry_concurrent_access_does_not_crosstalk() -> None:
    """并发读写不同键不得串扰：主线程预置的见证键对所有工作线程始终可见。"""

    rounds = 200
    errors: list[BaseException] = []
    witness_call_id = "call-witness"
    tool_call_cancellation_registry.mark_cancelled(_TASK_ID, witness_call_id)

    def _worker(offset: int) -> None:
        try:
            for index in range(rounds):
                call_id = f"call-{offset}-{index}"
                tool_call_cancellation_registry.mark_cancelled(_TASK_ID, call_id)
                assert tool_call_cancellation_registry.is_cancelled(_TASK_ID, call_id) is True
                # 并发写入不得挤掉见证键，也不得让本线程误命中别的 Task。
                assert tool_call_cancellation_registry.is_cancelled(_TASK_ID, witness_call_id) is True
                assert tool_call_cancellation_registry.is_cancelled(_OTHER_TASK, call_id) is False
                tool_call_cancellation_registry.clear(_TASK_ID, call_id)
        except BaseException as exc:  # 线程异常需回传主线程断言，不能静默丢失
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(offset,)) for offset in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert tool_call_cancellation_registry.is_cancelled(_TASK_ID, witness_call_id) is True
    assert tool_call_cancellation_registry.is_cancelled(_TASK_ID, "call-0-0") is False


# ---------------------------------------------------------------------------
# 2. 工具执行层
# ---------------------------------------------------------------------------


def test_tool_call_signal_cancels_only_the_named_call_in_parallel(tmp_path: Path) -> None:
    """并行执行时只中止被点名的调用：兄弟调用必须跑到终点。

    这是本需求的核心不变量——工具级取消只打断工具执行，不打断 Run 与同批其它调用。
    """

    cancelled_marker = tmp_path / "cancelled.txt"
    sibling_marker = tmp_path / "sibling.txt"
    tool = _make_tool(_handler_sleeps_then_writes, name="proc_parallel")
    observations: dict[str, Any] = {}

    def _execute(call_id: str, marker: Path, sleep_seconds: float) -> None:
        observations[call_id] = ToolHandlerRunner().execute(
            tool,
            {"marker_path": str(marker), "sleep_seconds": sleep_seconds},
            _make_context(_RUN_A, tool_call_id=call_id),
        )

    cancelled_thread = threading.Thread(
        target=_execute, args=("call-cancelled", cancelled_marker, 5.0)
    )
    sibling_thread = threading.Thread(target=_execute, args=("call-sibling", sibling_marker, 1.0))
    timer = threading.Timer(
        0.3, tool_call_cancellation_registry.mark_cancelled, args=(_TASK_ID, "call-cancelled")
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
    assert observations["call-cancelled"].content.startswith("cancelled: ")
    assert cancelled_elapsed < 2.5, f"被点名调用耗时 {cancelled_elapsed:.2f}s，疑似未被强杀"
    assert cancelled_marker.exists() is False

    assert observations["call-sibling"].status == "success"
    assert observations["call-sibling"].content == "completed"
    assert sibling_marker.exists() is True


def test_tool_call_signal_does_not_touch_run_level_registry() -> None:
    """工具级取消不得写入 Task 级注册表，否则会把整个 agent 执行打断。"""

    tool = _make_tool(_handler_sleeps_then_writes, name="proc_run_isolation")
    tool_call_cancellation_registry.mark_cancelled(_TASK_ID, "call-1")

    observation = ToolHandlerRunner().execute(
        tool,
        {"marker_path": "unused.txt", "sleep_seconds": 5.0},
        _make_context(_RUN_A, tool_call_id="call-1"),
    )

    assert observation.status == "cancelled"
    assert cancellation_registry.is_cancelled(_TASK_ID) is False


def _capture_workflow_emits(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """把工具批次的事件出口 ``tool_batch_runner.emit`` 替换为记录事件的替身。

    ``ToolHandlerRunner`` 与 ``ToolExecutor`` 都不投影 Transport 状态，running / 终态只由
    ``_execute_call`` 经 ``emit`` 发出。本替身用于断言「执行层不会触达事件出口」：若有人把
    执行期投影重新接回执行层，这个列表就会非空。
    """

    captured: list[Any] = []
    monkeypatch.setattr(tool_batch_runner, "emit", captured.append)
    return captured


def test_cancelled_execution_returns_cancelled_observation() -> None:
    """工具级取消命中时返回取消观察，且不向上抛异常。"""

    tool_call_cancellation_registry.mark_cancelled(_TASK_ID, "call-1")
    tool = _make_tool(_handler_sleeps_then_writes, name="proc_cancelled", execution_mode="thread")

    observation = ToolHandlerRunner().execute(
        tool,
        {"marker_path": "unused.txt", "sleep_seconds": 0.0},
        _make_context(_RUN_A, tool_call_id="call-1"),
    )

    assert observation.status == "cancelled"
    assert observation.content.startswith("cancelled: ")


def test_thread_mode_keeps_result_when_cancel_arrives_during_execution() -> None:
    """thread 模式：handler 已完成就保留真实结果，执行期间到达的取消不推翻已发生的副作用。"""

    tool = _make_tool(
        _handler_marks_own_call_signal, name="thread_post_boundary", execution_mode="thread"
    )

    observation = ToolHandlerRunner().execute(
        tool,
        {"task_id": _TASK_ID, "call_id": "call-post"},
        _make_context(_RUN_A, tool_call_id="call-post"),
    )

    assert observation.status == "success"


def test_runner_cancellation_does_not_project_transport_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消只产出观察与日志：执行层不得投影 Transport 终态。

    终态必须晚于强杀与输出排空，因此取消路径在整个执行期内不得触达工作流事件出口。第二层
    护栏断言旧的通知旁路与 tools 内的全局 projector 依赖本身已不存在，避免它们被重新
    引入而绕开该时序规则。
    """

    captured = _capture_workflow_emits(monkeypatch)
    tool_call_cancellation_registry.mark_cancelled(_TASK_ID, "call-1")
    tool = _make_tool(_handler_sleeps_then_writes, name="proc_no_bypass", execution_mode="thread")

    observation = ToolHandlerRunner().execute(
        tool,
        {"marker_path": "unused.txt", "sleep_seconds": 0.0},
        _make_context(_RUN_A, tool_call_id="call-1"),
    )

    assert observation.status == "cancelled"
    assert observation.content.startswith("cancelled: ")
    assert captured == []
    assert not hasattr(tool_handler_runner, "_notify_tool_cancelled")
    assert not hasattr(tool_handler_runner, "get_conversation_event_projector")
    assert not hasattr(tool_terminal_projection, "get_conversation_event_projector")
    assert not hasattr(tool_terminal_projection, "project_tool_terminal_state")


def test_terminal_projection_runs_only_after_pipeline_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终态投影必须晚于执行管线收尾：进程强杀与输出排空完成前不得变更前端状态。

    ``ToolHandlerRunner.execute`` 只有在强杀（``_force_kill`` 的 terminate / kill 两轮 join）与
    输出排空（输出通道 ``finish``）都做完之后才返回，``_execute_call`` 又只在
    ``executor.execute`` 返回后才发终态，因此「终态晚于 runner 返回」等价于「终态晚于进程
    收尾」——这是取消语义的核心不变量。running 由执行器在交给 runner 之前发出。
    """

    order: list[str] = []
    cancelled = tool_cancelled("probe_tool", tool_call_id="call-1")

    def _fake_runner_execute(
        _self: ToolHandlerRunner,
        _tool: ToolDefinition,
        _arguments: dict[str, Any],
        execution_context: ToolExecutionContext,
    ) -> Any:
        assert execution_context.tool_call_id == "call-1"
        order.append("runner_returned")
        return cancelled

    def _record_emit(event: Any) -> None:
        order.append(f"emit:{event.status}")

    monkeypatch.setattr(ToolHandlerRunner, "execute", _fake_runner_execute)
    monkeypatch.setattr(tool_batch_runner, "emit", _record_emit)
    monkeypatch.setattr(tool_executor_module, "emit", _record_emit)
    executor = ToolExecutor(
        registry=ToolRegistry([_make_tool(_handler_sleeps_then_writes, execution_mode="thread")])
    )
    rc = runtime_stub(
        run=SimpleNamespace(id=_RUN_A, task_id=_TASK_ID),
        task=SimpleNamespace(id=_TASK_ID),
        tool_executor=executor,
        allows_tools=frozenset({"probe_tool"}),
    )
    batch = ToolBatch(
        valid_tool_calls={"call-1": ValidToolCall(name="probe_tool", projection="projected")}
    )

    observation = tool_batch_runner._execute_call(
        rc,
        ToolCall(tool_name="probe_tool", arguments="{}", call_id="call-1"),
        "step-1",
        batch,
        _NO_LOOP,
    )

    assert observation.status == "cancelled"
    assert order == ["emit:running", "runner_returned", "emit:cancelled"]


def test_tool_call_signal_is_released_after_execution() -> None:
    """单次执行结束后（任意结果）工具级信号必须被释放，Task 级信号不受影响。"""

    tool_call_cancellation_registry.mark_cancelled(_TASK_ID, "call-1")
    cancellation_registry.request_cancel(_TASK_ID)
    tool = _make_tool(_handler_sleeps_then_writes, name="proc_release", execution_mode="thread")

    observation = ToolHandlerRunner().execute(
        tool,
        {"marker_path": "unused.txt", "sleep_seconds": 0.0},
        _make_context(_RUN_A, tool_call_id="call-1"),
    )

    assert observation.status == "cancelled"
    assert tool_call_cancellation_registry.is_cancelled(_TASK_ID, "call-1") is False
    assert cancellation_registry.is_cancelled(_TASK_ID) is True


def test_late_tool_call_signal_survives_until_execution_cleanup() -> None:
    """点名了已结束调用的迟到信号不会被工具执行层消费，只能由执行收尾兜底清理。"""

    tool_call_cancellation_registry.mark_cancelled(_TASK_ID, "call-already-finished")

    assert tool_call_cancellation_registry.is_cancelled(_TASK_ID, "call-already-finished") is True

    tool_call_cancellation_registry.clear_task(_TASK_ID)

    assert tool_call_cancellation_registry.is_cancelled(_TASK_ID, "call-already-finished") is False


def test_cancel_check_watches_both_signal_sources() -> None:
    """取消检查必须同时实时观察 Task 级与工具级信号。"""

    context = _make_context(_RUN_A, tool_call_id="call-1")

    def check() -> bool:
        return ToolHandlerRunner._is_cancelled(context)

    assert check() is False

    tool_call_cancellation_registry.mark_cancelled(_TASK_ID, "call-1")
    assert check() is True
    tool_call_cancellation_registry.clear(_TASK_ID, "call-1")
    assert check() is False

    cancellation_registry.request_cancel(_TASK_ID)
    assert check() is True
    cancellation_registry.consume(_TASK_ID)
    assert check() is False


# ---------------------------------------------------------------------------
# 3. 调度入口与 HTTP 端点
# ---------------------------------------------------------------------------


def test_cancel_tool_call_endpoint_is_registered() -> None:
    """工具级取消端点必须注册在真实 app 上，且与 Task 级取消端点同级。"""

    from app.app import app

    routes = {
        (route.path, tuple(sorted(getattr(route, "methods", None) or ()))) for route in app.routes
    }

    assert ("/tasks/{task_id}/tool-calls/{tool_call_id}/cancel", ("POST",)) in routes
    assert ("/tasks/{task_id}/cancel", ("POST",)) in routes


# ---------------------------------------------------------------------------
# 4. 取消事件的 snapshot 投影口径
# ---------------------------------------------------------------------------


def _running_tool_state(tool_call_id: str = "call-9") -> dict[str, Any]:
    """构造只含一个 ``running`` tool-call part 的最小 snapshot，供投影纯函数断言。"""

    return {
        "messages": [
            {
                "id": "m1",
                "role": "assistant",
                "runId": 4,
                "outcome": None,
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
        "activity": {
            "runId": 4,
            "status": "running",
            "endReason": None,
            "error": None,
            "usage": None,
            "contextWindowTotal": None,
            "launch": None,
        },
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
        mutation.path: mutation.value
        for mutation in plan_event(event, _running_tool_state("call-1"))
    }
    base = ("messages", 0, "parts", 0)

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

    assert list(plan_event(event, {"messages": [], "activity": None})) == []


def _tool_state_with_status(status: str, tool_call_id: str) -> dict[str, Any]:
    """构造只含一个指定状态 tool-call part 的最小 snapshot，供终态迁移白名单断言。"""

    state = _running_tool_state(tool_call_id)
    state["messages"][0]["parts"][0]["status"] = status
    return state


def test_placeholder_overwrite_transition_cancelled_to_completed_is_applied() -> None:
    """``cancelled -> completed`` 必须放行：占位被真实结果覆盖后前端要翻到真实终态。

    链路：取消/崩溃时 ``RuntimeMessageContextManager.load_message`` 会为未配对调用补 ``cancelled``
    占位（投影为 cancelled part），该 run 续跑重放后真实结果经 ``add_message`` 按配对规则命中
    该行并原地覆盖
    同一行，结算事件携带 completed。若投影层丢弃该迁移，数据库已是真实终态而 snapshot 会永久
    停在 cancelled。
    """

    event = ToolCallStatusChangedEvent(
        task_id=11,
        run_id=_RUN_A,
        tool_call_id="call-1",
        status="completed",
        display_data={"kind": "read-file-meta", "path": "a.py"},
    )

    mutations = {
        mutation.path: mutation.value
        for mutation in plan_event(event, _tool_state_with_status("cancelled", "call-1"))
    }
    base = ("messages", 0, "parts", 0)

    assert mutations[(*base, "status")] == "completed"
    assert mutations[(*base, "isError")] is False
    assert mutations[(*base, "display_data")] == {"kind": "read-file-meta", "path": "a.py"}


def test_late_event_cannot_revive_a_completed_part_and_is_logged(caplog: Any) -> None:
    """``completed`` 不可逆：迟到事件被丢弃且必须留 warning（不得静默失效）。"""

    event = ToolCallStatusChangedEvent(
        task_id=11,
        run_id=_RUN_A,
        tool_call_id="call-1",
        status="failed",
        error="迟到失败",
    )

    with caplog.at_level("WARNING"):
        mutations = list(plan_event(event, _tool_state_with_status("completed", "call-1")))

    assert mutations == []
    assert [
        record.getMessage()
        for record in caplog.records
        if record.getMessage() == "tool_call_status_transition_dropped"
    ], "丢弃终态迁移必须留下可排查日志"
