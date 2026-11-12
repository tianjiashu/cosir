"""后台结果登记表、结论投递与 ``wait_messages`` 判定。

终端服务本身不参与：条目由执行单元登记（这里直接调用 ``command_results``）。重启收尾见
``test_recover_after_restart.py`` 与 ``test_background_placeholders.py``。
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from app.core.tool.handler.wait_messages import WaitMessagesTool
from app.core.tool.schemas import ToolExecutionContext
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.core.workflow.message_queue.task_message import BackgroundResultBody
from app.service import depends
from app.tools.tool_handler.terminal import command_results as background_module
from app.tools.tool_handler.terminal.command_job import CommandConclusion
from app.tools.tool_handler.terminal.command_results import (
    CommandResultRegistry,
    command_results,
    deliver_background_result,
)
from tests.live_cold_support import registered


@pytest.fixture(autouse=True)
def _clean_entries():
    yield
    for task_id in {entry.task_id for entry in command_results._entries.values()}:
        command_results.drop_task(task_id)


# ── 登记表 ───────────────────────────────────────────────────────────────


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str | None, str]]:
    calls: list[tuple[int, str | None, str]] = []

    def deliver(
        task_id: int,
        *,
        tool_call_id: str | None,
        tool_name: str,
        result: dict[str, object],
        placeholder_display_data: dict[str, object] | None = None,
    ) -> bool:
        calls.append((task_id, tool_call_id, tool_name))
        return True

    monkeypatch.setattr(background_module, "deliver_background_result", deliver)
    return calls


def _register(
    registry: CommandResultRegistry, tool_call_id: str, task_id: int, label: str, on_cancel=None
) -> str:
    registry.register(
        tool_call_id,
        task_id=task_id,
        run_id=1,
        kind="terminal_command",
        label=label,
        on_cancel=lambda _reason: on_cancel() if on_cancel else None,
    )
    return tool_call_id


def test_complete_is_pop_once_and_carries_the_call(delivered) -> None:
    registry = CommandResultRegistry()
    _register(registry, "call-a", 1, "terminal:a")
    assert registry.pending(1) == ["terminal:a"]
    assert registry.complete(1, "call-a", CommandConclusion({})) is True
    assert registry.complete(1, "call-a", CommandConclusion({})) is False
    assert registry.pending(1) == []
    assert delivered == [(1, "call-a", "terminal_command")]


def test_has_work_covers_the_whole_task_and_results_still_being_written(monkeypatch) -> None:
    """后台工作属于 Task：更早 Run 登记的也算；结论已出队、还在写消息队列时同样算有事可做。"""
    entered, release = threading.Event(), threading.Event()

    def slow_deliver(*_args: object, **_kwargs: object) -> bool:
        entered.set()
        assert release.wait(5)
        return True

    monkeypatch.setattr(background_module, "deliver_background_result", slow_deliver)
    registry = CommandResultRegistry()
    for call_id, run_id in (("call-old", 1), ("call-new", 2)):
        registry.register(
            call_id, task_id=1, run_id=run_id, kind="terminal_command", label=call_id,
            on_cancel=lambda _reason: None,
        )
    assert registry.pending(1) == ["call-old", "call-new"]
    assert registry.has_work(1) and not registry.has_work(2)

    registry.withdraw(1, "call-new")
    worker = threading.Thread(
        target=registry.complete, args=(1, "call-old", CommandConclusion({}))
    )
    worker.start()
    assert entered.wait(5)
    assert registry.live_background(1) == {}
    assert registry.has_work(1)
    release.set()
    worker.join(5)
    assert not registry.has_work(1)


def test_duplicate_registration_is_rejected(delivered) -> None:
    registry = CommandResultRegistry()
    _register(registry, "call-a", 1, "a")
    with pytest.raises(ValueError):
        _register(registry, "call-a", 1, "b")
    assert registry.pending(1) == ["a"]


def test_close_cancels_everything_and_blocks_later_delivery(delivered) -> None:
    registry = CommandResultRegistry()
    cancelled: list[str] = []
    first = _register(registry, "call-a", 1, "a", lambda: cancelled.append("a"))
    _register(registry, "call-b", 2, "b", lambda: cancelled.append("b"))
    registry.close()
    assert sorted(cancelled) == ["a", "b"]
    assert registry.complete(1, first, CommandConclusion({})) is False
    late = _register(registry, "call-late", 1, "late")
    assert registry.pending(1) == []
    assert registry.complete(1, late, CommandConclusion({})) is False
    assert delivered == []
    registry.open()
    reopened = _register(registry, "call-c", 1, "c")
    assert registry.complete(1, reopened, CommandConclusion({})) is True


@pytest.mark.parametrize("user_cancel", [False, True], ids=["complete", "single_cancel"])
@pytest.mark.parametrize("delivery_fails", [False, True], ids=["written", "write_failed"])
def test_close_waits_for_in_flight_delivery(
    monkeypatch: pytest.MonkeyPatch, user_cancel: bool, delivery_fails: bool,
) -> None:
    """正常完成与单停都要等回执写完；写入失败也必须释放关闭等待。"""
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    closing_started = threading.Event()

    def slow_deliver(*_args: object, **_kwargs: object) -> bool:
        entered.set()
        assert release.wait(5)
        finished.set()
        if delivery_fails:
            raise RuntimeError("receipt write failed")
        return True

    monkeypatch.setattr(
        background_module,
        "report_cancelled_result" if user_cancel else "deliver_background_result",
        slow_deliver,
    )
    registry = CommandResultRegistry()
    cancelled: list[str] = []
    _register(registry, "call-a", 1, "a", lambda: cancelled.append("a"))

    def deliver() -> bool:
        if user_cancel:
            return registry.request_cancel(1, "call-a", driving=False)
        return registry.complete(1, "call-a", CommandConclusion({}))

    def close() -> bool:
        closing_started.set()
        registry.close()
        return finished.is_set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        deliverer = executor.submit(deliver)
        try:
            assert entered.wait(2)
            assert not registered(registry, 1, "call-a")
            assert not registry.complete(1, "call-a", CommandConclusion({}))
            assert not registry.request_cancel(1, "call-a", driving=False)
            closer = executor.submit(close)
            assert closing_started.wait(2)
            with pytest.raises(TimeoutError):
                closer.result(timeout=0.2)
        finally:
            release.set()
        assert closer.result(timeout=2)
        if delivery_fails:
            with pytest.raises(RuntimeError, match="receipt write failed"):
                deliverer.result(timeout=2)
        else:
            assert deliverer.result(timeout=2)
    assert cancelled == (["a"] if user_cancel else [])


def test_drop_task_runs_every_callback_even_when_one_fails(delivered) -> None:
    registry = CommandResultRegistry()
    called: list[str] = []

    def boom() -> None:
        called.append("boom")
        raise RuntimeError("cleanup failed")

    first = _register(registry, "call-a", 1, "a", boom)
    second = _register(registry, "call-b", 1, "b", lambda: called.append("b"))
    kept = _register(registry, "call-c", 2, "c", lambda: called.append("c"))
    registry.drop_task(1)
    assert sorted(called) == ["b", "boom"]
    assert registry.complete(1, first, CommandConclusion({})) is False
    assert registry.complete(1, second, CommandConclusion({})) is False
    assert registry.complete(2, kept, CommandConclusion({})) is True


# ── 结论投递 ─────────────────────────────────────────────────────────────


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, Any]]:
    from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

    calls: list[tuple[int, Any]] = []

    def enqueue(task_id: int, message: Any, **_kw: Any) -> int:
        if task_id == 404:
            raise KeyError(task_id)
        calls.append((task_id, message))
        return len(calls)

    monkeypatch.setattr(task_runtime_spaces, "enqueue", enqueue)
    return calls


def test_deliver_enqueues_background_result(enqueued: list[tuple[int, Any]], monkeypatch) -> None:
    monkeypatch.setattr(background_module, "_now", lambda: "2026-01-01T00:01:00+00:00")
    assert deliver_background_result(
        5, tool_call_id="call-5", tool_name="terminal_command",
        result={"end_reason": "condition_met"},
    )
    [(task_id, message)] = enqueued
    assert task_id == 5
    assert isinstance(message.body, BackgroundResultBody)
    assert message.body.tool_name == "terminal_command"
    assert message.body.tool_call_id == "call-5"
    assert message.body.ended_at == "2026-01-01T00:01:00+00:00"
    rendered, _ = message.body.render()
    assert 'tool_call_id="call-5"' in rendered["content"]
    assert json.loads(message.body.tool_result) == {"end_reason": "condition_met"}


def test_complete_while_stopping_leaves_entry_for_drop(delivered) -> None:
    """停止期间到达的结论不投递、不出队：条目留给收尾的 ``drop_task``，结局记为撤销。"""
    registry = CommandResultRegistry()
    cancelled: list[str] = []
    _register(registry, "call-a", 6, "a", lambda: cancelled.append("a"))
    cancellation_registry.request_cancel(6, "user")
    try:
        assert registry.complete(1, "call-a", CommandConclusion({})) is False
        assert registry.pending(6) == ["a"]
        [dropped] = registry.drop_task(6)
    finally:
        cancellation_registry.consume(6)
    assert dropped == "call-a"
    assert cancelled == ["a"]
    assert registry.live_background(6) == {}
    assert delivered == []


def test_deliver_to_deleted_task_returns_false(enqueued: list[tuple[int, Any]]) -> None:
    assert not deliver_background_result(
        404, tool_call_id=None, tool_name="browser_handoff", result={}
    )


def test_result_without_call_omits_the_attribute() -> None:
    body = BackgroundResultBody(
        ended_at="2026-01-01T00:01:00+00:00", tool_name="terminal_command", tool_result="{}"
    )
    rendered, _ = body.render()
    assert "tool_call_id" not in rendered["content"]


# ── wait_messages ────────────────────────────────────────────────────────


@pytest.mark.usefixtures("isolated_storage")
def test_wait_messages_errors_when_idle_and_suspends_on_pending_entry(tmp_path: Path) -> None:
    workspace = depends.get_workspace_crud().create("ws", str(tmp_path))
    task = depends.get_task_crud().create(workspace.id, "task")
    context = ToolExecutionContext(
        task_id=task.id,
        workspace_id=workspace.id,
        workspace_root=tmp_path,
        step_id="step-1",
        run_id=1,
    )
    tool = WaitMessagesTool()

    idle = tool.execute(context)
    assert idle.status == "error"
    assert not idle.suspend
    assert "nothing to wait for" in idle.content

    _register(command_results, "call-1", task.id, "terminal_command:term_1")
    waiting = tool.execute(context)
    assert waiting.suspend
    assert "terminal_command:term_1" in waiting.content


def test_wait_messages_description_mentions_terminal_work() -> None:
    description = WaitMessagesTool().description
    assert "terminal" in description
    assert "child agent" in description and "<agent_message" in description


# ── 前台 / 后台、取消与看板 ─────────────────────────────────────────────


def _register_foreground(
    registry: CommandResultRegistry, tool_call_id: str, calls: list[str],
) -> threading.Event:
    detached = threading.Event()
    registry.register(
        tool_call_id,
        task_id=1,
        run_id=None,
        kind="terminal_command",
        label="terminal_command:t",
        mode="foreground",
        on_cancel=lambda _reason: calls.append("cancel"),
        detached=detached,
    )
    return detached


def test_foreground_settles_when_not_detached(delivered) -> None:
    registry = CommandResultRegistry()
    calls: list[str] = []
    _register_foreground(registry, "call-f", calls)
    assert registry.settle_foreground(1, "call-f") == "settled"
    assert registry.pending(1) == []
    assert delivered == [] and calls == []
    # 前台等待只是一次普通工具调用：不进看板，结束后也不留记录。
    assert registry.live_background(1) == {} and registry.foreground_calls(1) == []


def test_foreground_entries_only_surface_as_detachable_calls(delivered) -> None:
    registry = CommandResultRegistry()
    calls: list[str] = []
    _register_foreground(registry, "call-f", calls)
    _register(registry, "call-b", 1, "b")
    assert list(registry.live_background(1)) == ["call-b"]
    assert registry.foreground_calls(1) == ["call-f"]
    assert registered(registry, 1, "call-f") and not registered(registry, 2, "call-f")
    # 转到后台后离开前台列表，成为活后台工作；结束后登记表不保留历史。
    registry.detach(1, "call-f")
    assert registry.foreground_calls(1) == []
    assert list(registry.live_background(1)) == ["call-f", "call-b"]
    registry.complete(1, "call-f", CommandConclusion({}))
    assert list(registry.live_background(1)) == ["call-b"]


def test_cancelled_foreground_entries_leave_no_history(delivered) -> None:
    registry = CommandResultRegistry()
    calls: list[str] = []
    _register_foreground(registry, "call-f", calls)
    assert registry.withdraw(1, "call-f")
    assert calls == ["cancel"] and registry.live_background(1) == {}


def test_detach_wins_over_a_later_foreground_settle(delivered) -> None:
    registry = CommandResultRegistry()
    calls: list[str] = []
    detached = _register_foreground(registry, "call-f", calls)
    assert registry.detach(1, "call-f") == "detached"
    assert detached.is_set()
    # 等待线程随后结束：转后台已先到，结论必须改走后台回投。
    assert registry.settle_foreground(1, "call-f") == "detached"
    assert registry.complete(1, "call-f", CommandConclusion({})) is True
    assert delivered == [(1, "call-f", "terminal_command")]


def test_detach_only_applies_to_live_foreground_entries(delivered) -> None:
    registry = CommandResultRegistry()
    _register(registry, "call-b", 1, "b")
    assert registry.detach(1, "call-b") == "not_foreground"
    assert registry.detach(1, "call-missing") == "not_found"
    calls: list[str] = []
    _register_foreground(registry, "call-f", calls)
    registry.withdraw(1, "call-f")
    assert registry.settle_foreground(1, "call-f") == "gone"


def test_user_cancel_reports_the_mode_and_removes_live_work(delivered) -> None:
    registry = CommandResultRegistry()
    called: list[str] = []
    _register(registry, "call-b", 1, "b", lambda: called.append("b"))
    dropped = registry.withdraw(1, "call-b")
    assert dropped
    assert called == ["b"]
    assert not registry.withdraw(1, "call-b")
    assert registry.live_background(1) == {}


def test_live_background_excludes_finished_work(delivered) -> None:
    registry = CommandResultRegistry()
    for index in range(3):
        _register(registry, f"call-{index}", 1, f"l{index}")
        registry.complete(1, f"call-{index}", CommandConclusion({}))
    _register(registry, "call-live", 1, "live")
    assert list(registry.live_background(1)) == ["call-live"]


def test_drop_task_reports_dropped_entries(delivered) -> None:
    registry = CommandResultRegistry()
    _register(registry, "call-b", 1, "b")
    calls: list[str] = []
    _register_foreground(registry, "call-f", calls)
    dropped = registry.drop_task(1)
    assert dropped == {"call-b": None}


def test_listener_is_notified_off_the_caller_thread(delivered) -> None:
    import threading

    registry = CommandResultRegistry()
    seen: list[tuple[int, str]] = []
    notified = threading.Event()

    def listener(task_id: int) -> None:
        seen.append((task_id, threading.current_thread().name))
        notified.set()

    registry.set_listener(listener)
    _register(registry, "call-a", 7, "a")
    assert notified.wait(2)
    task_id, thread_name = seen[0]
    assert task_id == 7 and thread_name.startswith("background-notify")


def test_foreground_waits_contract() -> None:
    from app.assistant_transport.state.conversation_state_background_task import (
        validate_foreground_waits,
    )

    validate_foreground_waits(["call-a", "call-b"])
    for invalid in (None, [""], ["a", "a"], [1]):
        with pytest.raises(ValueError):
            validate_foreground_waits(invalid)


def test_single_stop_before_registration_is_taken_over_by_the_entry():
    """登记前就被点名停止的调用：登记时接过工具级信号，作业立即收到停止，之后的重复停止被拒。"""
    from app.core.workflow.cancel.tool_call_cancellation_registry import (
        tool_call_cancellation_registry,
    )

    registry = CommandResultRegistry()
    signals = []
    try:
        assert registry.request_cancel(3, "early", driving=True)
        registry.register(
            "early", task_id=3, run_id=5, kind="terminal_command", label="build",
            on_cancel=signals.append, reports_user_stop=True,
        )
        assert signals == ["user"]
        assert not registry.request_cancel(3, "early", driving=True)
    finally:
        tool_call_cancellation_registry.clear(3, "early")
        registry.close()
