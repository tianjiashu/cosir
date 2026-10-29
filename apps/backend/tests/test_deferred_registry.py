"""延迟结果登记表、结论投递与 ``wait_messages`` 判定。

终端服务本身不参与：条目由执行单元登记（这里直接调用 ``deferred_registry``）。重启收尾见
``test_restart_settlement.py``。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.core.tool.background import deferred_registry as deferred_module
from app.core.tool.background.deferred_registry import (
    DeferredRegistry,
    deferred_registry,
    deliver_background_result,
)
from app.core.tool.handler.wait_messages import WaitMessagesTool
from app.core.tool.schemas import ToolExecutionContext
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.core.workflow.message_queue.task_message import BackgroundResultBody
from app.service import depends


@pytest.fixture(autouse=True)
def _clean_entries():
    yield
    for task_id in {entry.task_id for entry in deferred_registry._entries.values()}:
        deferred_registry.drop_task(task_id)


# ── 登记表 ───────────────────────────────────────────────────────────────


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str | None, str]]:
    calls: list[tuple[int, str | None, str]] = []

    def deliver(
        task_id: int, *, tool_call_id: str | None, tool_name: str, result: dict[str, object]
    ) -> bool:
        calls.append((task_id, tool_call_id, tool_name))
        return True

    monkeypatch.setattr(deferred_module, "deliver_background_result", deliver)
    return calls


def _register(
    registry: DeferredRegistry, tool_call_id: str, task_id: int, label: str, on_cancel=None
) -> str:
    registry.register(
        tool_call_id,
        task_id=task_id,
        run_id=None,
        kind="terminal_command",
        label=label,
        on_cancel=on_cancel or (lambda: None),
    )
    return tool_call_id


def test_complete_is_pop_once_and_carries_the_call(delivered) -> None:
    registry = DeferredRegistry()
    _register(registry, "call-a", 1, "terminal:a")
    assert registry.pending(1) == ["terminal:a"]
    assert registry.complete("call-a", {}) is True
    assert registry.complete("call-a", {}) is False
    assert registry.pending(1) == []
    assert delivered == [(1, "call-a", "terminal_command")]


def test_duplicate_registration_is_rejected(delivered) -> None:
    registry = DeferredRegistry()
    _register(registry, "call-a", 1, "a")
    with pytest.raises(ValueError):
        _register(registry, "call-a", 1, "b")
    assert registry.pending(1) == ["a"]


def test_close_cancels_everything_and_blocks_later_delivery(delivered) -> None:
    registry = DeferredRegistry()
    cancelled: list[str] = []
    first = _register(registry, "call-a", 1, "a", lambda: cancelled.append("a"))
    _register(registry, "call-b", 2, "b", lambda: cancelled.append("b"))
    registry.close()
    assert sorted(cancelled) == ["a", "b"]
    assert registry.complete(first, {}) is False
    late = _register(registry, "call-late", 1, "late")
    assert registry.pending(1) == []
    assert registry.complete(late, {}) is False
    assert delivered == []
    registry.open()
    reopened = _register(registry, "call-c", 1, "c")
    assert registry.complete(reopened, {}) is True


def test_close_waits_for_in_flight_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """关闭返回后不得再有写库：在途投递要先落定。"""
    import threading

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow_deliver(
        task_id: int, *, tool_call_id: str | None, tool_name: str, result: dict[str, object]
    ) -> bool:
        entered.set()
        assert release.wait(5)
        finished.set()
        return True

    monkeypatch.setattr(deferred_module, "deliver_background_result", slow_deliver)
    registry = DeferredRegistry()
    _register(registry, "call-a", 1, "a")
    deliverer = threading.Thread(target=lambda: registry.complete("call-a", {}))
    deliverer.start()
    assert entered.wait(2)
    closer = threading.Thread(target=registry.close)
    closer.start()
    closer.join(0.2)
    assert closer.is_alive()
    release.set()
    closer.join(2)
    assert not closer.is_alive() and finished.is_set()
    deliverer.join(2)


def test_drop_task_runs_every_callback_even_when_one_fails(delivered) -> None:
    registry = DeferredRegistry()
    called: list[str] = []

    def boom() -> None:
        called.append("boom")
        raise RuntimeError("cleanup failed")

    first = _register(registry, "call-a", 1, "a", boom)
    second = _register(registry, "call-b", 1, "b", lambda: called.append("b"))
    kept = _register(registry, "call-c", 2, "c", lambda: called.append("c"))
    registry.drop_task(1)
    assert sorted(called) == ["b", "boom"]
    assert registry.complete(first, {}) is False
    assert registry.complete(second, {}) is False
    assert registry.complete(kept, {}) is True


def test_discard_removes_without_calling_cancel(delivered) -> None:
    registry = DeferredRegistry()
    called: list[str] = []
    _register(registry, "call-a", 1, "a", lambda: called.append("a"))
    registry.discard("call-a")
    registry.discard("call-missing")
    assert registry.pending(1) == []
    assert called == []
    assert registry.complete("call-a", {}) is False


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


def test_deliver_enqueues_background_result(enqueued: list[tuple[int, Any]]) -> None:
    assert deliver_background_result(
        5, tool_call_id="call-5", tool_name="terminal_command", result={"end_reason": "condition_met"}
    )
    [(task_id, message)] = enqueued
    assert task_id == 5
    assert isinstance(message.body, BackgroundResultBody)
    assert message.body.tool_name == "terminal_command"
    assert message.body.tool_call_id == "call-5"
    rendered, _ = message.body.render()
    assert 'tool_call_id="call-5"' in rendered["content"]
    assert json.loads(message.body.tool_result) == {"end_reason": "condition_met"}


def test_deliver_is_dropped_while_task_is_stopping(enqueued: list[tuple[int, Any]]) -> None:
    cancellation_registry.request_cancel(6, "user")
    try:
        assert not deliver_background_result(
            6, tool_call_id=None, tool_name="terminal_command", result={}
        )
    finally:
        cancellation_registry.consume(6)
    assert enqueued == []


def test_deliver_to_deleted_task_returns_false(enqueued: list[tuple[int, Any]]) -> None:
    assert not deliver_background_result(
        404, tool_call_id=None, tool_name="terminal_handoff", result={}
    )


def test_result_without_call_omits_the_attribute() -> None:
    body = BackgroundResultBody(tool_name="terminal_command", tool_result="{}")
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

    _register(deferred_registry, "call-1", task.id, "terminal_command:term_1")
    waiting = tool.execute(context)
    assert waiting.suspend
    assert "terminal_command:term_1" in waiting.content


def test_wait_messages_description_mentions_terminal_work() -> None:
    description = WaitMessagesTool().description
    assert "terminal" in description
    assert "child agents" in description


# ── 前台 / 后台、取消与看板 ─────────────────────────────────────────────


def _register_foreground(registry: DeferredRegistry, tool_call_id: str, calls: list[str]) -> None:
    registry.register(
        tool_call_id,
        task_id=1,
        run_id=None,
        kind="terminal_command",
        label="terminal_command:t",
        mode="foreground",
        on_cancel=lambda: calls.append("cancel"),
        on_detach=lambda: calls.append("detach"),
    )


def test_foreground_settles_when_not_detached(delivered) -> None:
    registry = DeferredRegistry()
    calls: list[str] = []
    _register_foreground(registry, "call-f", calls)
    assert registry.settle_foreground("call-f") == "settled"
    assert registry.pending(1) == []
    assert delivered == [] and calls == []
    # 前台等待只是一次普通工具调用：不进看板，结束后也不留记录。
    assert registry.entries(1) == [] and registry.foreground_calls(1) == []


def test_foreground_entries_only_surface_as_detachable_calls(delivered) -> None:
    registry = DeferredRegistry()
    calls: list[str] = []
    _register_foreground(registry, "call-f", calls)
    _register(registry, "call-b", 1, "b")
    assert [view.tool_call_id for view in registry.entries(1)] == ["call-b"]
    assert registry.foreground_calls(1) == ["call-f"]
    assert registry.owns(1, "call-f") and not registry.owns(2, "call-f")
    # 转到后台后才算后台工作：离开前台列表、进入看板，结束后留在最近结束里。
    registry.detach("call-f")
    assert registry.foreground_calls(1) == []
    assert [view.tool_call_id for view in registry.entries(1)] == ["call-f", "call-b"]
    registry.complete("call-f", {})
    assert [(v.tool_call_id, v.status) for v in registry.entries(1)] == [
        ("call-b", "running"),
        ("call-f", "completed"),
    ]


def test_cancelled_foreground_entries_leave_no_history(delivered) -> None:
    registry = DeferredRegistry()
    calls: list[str] = []
    _register_foreground(registry, "call-f", calls)
    assert registry.cancel("call-f") is not None
    assert calls == ["cancel"] and registry.entries(1) == []


def test_detach_wins_over_a_later_foreground_settle(delivered) -> None:
    registry = DeferredRegistry()
    calls: list[str] = []
    _register_foreground(registry, "call-f", calls)
    assert registry.detach("call-f") == "detached"
    assert calls == ["detach"]
    # 等待线程随后结束：转后台已先到，结论必须改走后台回投。
    assert registry.settle_foreground("call-f") == "detached"
    assert registry.complete("call-f", {}) is True
    assert delivered == [(1, "call-f", "terminal_command")]


def test_detach_only_applies_to_live_foreground_entries(delivered) -> None:
    registry = DeferredRegistry()
    _register(registry, "call-b", 1, "b")
    assert registry.detach("call-b") == "not_foreground"
    assert registry.detach("call-missing") == "not_found"
    calls: list[str] = []
    _register_foreground(registry, "call-f", calls)
    registry.cancel("call-f")
    assert registry.settle_foreground("call-f") == "gone"


def test_user_cancel_reports_the_mode_and_records_history(delivered) -> None:
    registry = DeferredRegistry()
    called: list[str] = []
    _register(registry, "call-b", 1, "b", lambda: called.append("b"))
    dropped = registry.cancel("call-b")
    assert dropped is not None and (dropped.mode, dropped.task_id) == ("background", 1)
    assert called == ["b"]
    assert registry.cancel("call-b") is None
    [ended] = registry.entries(1)
    assert (ended.tool_call_id, ended.status) == ("call-b", "cancelled")
    assert ended.ended_at is not None


def test_entries_list_live_first_then_bounded_history(delivered, monkeypatch) -> None:
    monkeypatch.setattr(deferred_module, "MAX_ENDED_PER_TASK", 2)
    registry = DeferredRegistry()
    for index in range(3):
        _register(registry, f"call-{index}", 1, f"l{index}")
        registry.complete(f"call-{index}", {})
    _register(registry, "call-live", 1, "live")
    views = registry.entries(1)
    assert [view.tool_call_id for view in views] == ["call-live", "call-1", "call-2"]
    assert views[0].status == "running" and views[0].ended_at is None
    registry.forget_task(1)
    assert [view.tool_call_id for view in registry.entries(1)] == ["call-live"]


def test_drop_task_reports_dropped_entries(delivered) -> None:
    registry = DeferredRegistry()
    _register(registry, "call-b", 1, "b")
    calls: list[str] = []
    _register_foreground(registry, "call-f", calls)
    dropped = registry.drop_task(1)
    assert sorted((entry.tool_call_id, entry.mode) for entry in dropped) == [
        ("call-b", "background"),
        ("call-f", "foreground"),
    ]


def test_listener_is_notified_off_the_caller_thread(delivered) -> None:
    import threading

    registry = DeferredRegistry()
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


def test_snapshot_projection_matches_the_wire_contract(delivered) -> None:
    from app.assistant_transport.state.conversation_state_background_task import (
        project_background_task,
        validate_background_tasks,
    )

    registry = DeferredRegistry()
    _register(registry, "call-a", 1, "terminal_command:t")
    registry.complete("call-a", {})
    _register(registry, "call-b", 1, "child_task:2")
    entries = [project_background_task(view) for view in registry.entries(1)]
    validate_background_tasks(entries)
    assert [(e["toolCallId"], e["status"]) for e in entries] == [
        ("call-b", "running"),
        ("call-a", "completed"),
    ]
    with pytest.raises(ValueError):
        validate_background_tasks([{**entries[0], "mode": "background"}])


def test_foreground_waits_contract() -> None:
    from app.assistant_transport.state.conversation_state_background_task import (
        validate_foreground_waits,
    )

    validate_foreground_waits(["call-a", "call-b"])
    for invalid in (None, [""], ["a", "a"], [1]):
        with pytest.raises(ValueError):
            validate_foreground_waits(invalid)
