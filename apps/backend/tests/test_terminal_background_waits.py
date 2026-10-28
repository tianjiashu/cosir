"""后台等待登记、结论投递、``wait_messages`` 判定与重启后的旧终端失效告知。

终端服务本身不参与：后台等待由工具层登记（这里直接调用 ``background_watches``），重启唤醒与首次
驱动提醒只依赖 Run 状态与 context 里的工具调用事实。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.tool.handler.wait_messages import WaitMessagesTool
from app.core.tool.schemas import ToolExecutionContext
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.core.workflow.inbox.task_message import BackgroundResultBody, NoticeBody
from app.core.workflow.wait import background_watches as watches_module
from app.core.workflow.wait import terminal_invalidation
from app.core.workflow.wait.background_watches import (
    BackgroundWatches,
    background_watches,
    deliver_background_result,
)
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.service import depends


@pytest.fixture(autouse=True)
def _clean_watches():
    yield
    for task_id in {watch.task_id for watch in background_watches._entries.values()}:
        background_watches.drop_task(task_id)


# ── 登记表 ───────────────────────────────────────────────────────────────


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    calls: list[tuple[int, str]] = []

    def deliver(task_id: int, *, tool_name: str, result: dict[str, object]) -> bool:
        calls.append((task_id, tool_name))
        return True

    monkeypatch.setattr(watches_module, "deliver_background_result", deliver)
    return calls


def test_deliver_is_pop_once(delivered: list[tuple[int, str]]) -> None:
    watches = BackgroundWatches()
    watch_id = watches.register(1, run_id=3, label="terminal:a", on_cancel=lambda: None)
    assert watches.pending(1) == ["terminal:a"]
    assert watches.deliver(watch_id, tool_name="terminal_wait", result={}) is True
    assert watches.deliver(watch_id, tool_name="terminal_wait", result={}) is False
    assert watches.pending(1) == []
    assert delivered == [(1, "terminal_wait")]


def test_close_cancels_everything_and_blocks_later_delivery(
    delivered: list[tuple[int, str]],
) -> None:
    watches = BackgroundWatches()
    cancelled: list[str] = []
    first = watches.register(1, run_id=None, label="a", on_cancel=lambda: cancelled.append("a"))
    watches.register(2, run_id=None, label="b", on_cancel=lambda: cancelled.append("b"))
    watches.close()
    assert sorted(cancelled) == ["a", "b"]
    assert watches.deliver(first, tool_name="terminal_wait", result={}) is False
    late = watches.register(1, run_id=None, label="late", on_cancel=lambda: None)
    assert watches.pending(1) == []
    assert watches.deliver(late, tool_name="terminal_wait", result={}) is False
    assert delivered == []
    watches.open()
    reopened = watches.register(1, run_id=None, label="c", on_cancel=lambda: None)
    assert watches.deliver(reopened, tool_name="terminal_wait", result={}) is True


def test_close_waits_for_in_flight_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """关闭返回后不得再有写库：在途投递要先落定。"""
    import threading

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow_deliver(task_id: int, *, tool_name: str, result: dict[str, object]) -> bool:
        entered.set()
        assert release.wait(5)
        finished.set()
        return True

    monkeypatch.setattr(watches_module, "deliver_background_result", slow_deliver)
    watches = BackgroundWatches()
    watch_id = watches.register(1, run_id=None, label="a", on_cancel=lambda: None)
    deliverer = threading.Thread(
        target=lambda: watches.deliver(watch_id, tool_name="terminal_wait", result={})
    )
    deliverer.start()
    assert entered.wait(2)
    closer = threading.Thread(target=watches.close)
    closer.start()
    closer.join(0.2)
    assert closer.is_alive()
    release.set()
    closer.join(2)
    assert not closer.is_alive() and finished.is_set()
    deliverer.join(2)


def test_drop_task_runs_every_callback_even_when_one_fails(
    delivered: list[tuple[int, str]],
) -> None:
    watches = BackgroundWatches()
    called: list[str] = []

    def boom() -> None:
        called.append("boom")
        raise RuntimeError("cleanup failed")

    first = watches.register(1, run_id=None, label="a", on_cancel=boom)
    second = watches.register(1, run_id=None, label="b", on_cancel=lambda: called.append("b"))
    kept = watches.register(2, run_id=None, label="c", on_cancel=lambda: called.append("c"))
    watches.drop_task(1)
    assert sorted(called) == ["b", "boom"]
    assert watches.deliver(first, tool_name="t", result={}) is False
    assert watches.deliver(second, tool_name="t", result={}) is False
    assert watches.deliver(kept, tool_name="t", result={}) is True


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
        5, tool_name="terminal_wait", result={"end_reason": "condition_met"}
    )
    [(task_id, message)] = enqueued
    assert task_id == 5
    assert isinstance(message.body, BackgroundResultBody)
    assert message.body.tool_name == "terminal_wait"
    assert json.loads(message.body.tool_result) == {"end_reason": "condition_met"}


def test_deliver_is_dropped_while_task_is_stopping(enqueued: list[tuple[int, Any]]) -> None:
    cancellation_registry.request_cancel(6, "user")
    try:
        assert not deliver_background_result(6, tool_name="terminal_wait", result={})
    finally:
        cancellation_registry.consume(6)
    assert enqueued == []


def test_deliver_to_deleted_task_returns_false(enqueued: list[tuple[int, Any]]) -> None:
    assert not deliver_background_result(404, tool_name="terminal_handoff", result={})


def test_module_singleton_is_the_shared_registry() -> None:
    assert watches_module.background_watches is background_watches


# ── wait_messages ────────────────────────────────────────────────────────


@pytest.mark.usefixtures("isolated_storage")
def test_wait_messages_suspends_on_pending_terminal_wait(tmp_path: Path) -> None:
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
    assert idle.status == "success"
    assert not idle.suspend

    background_watches.register(
        task.id, run_id=1, label="terminal_wait:term_1", on_cancel=lambda: None
    )
    waiting = tool.execute(context)
    assert waiting.suspend
    assert "terminal_wait:term_1" in waiting.content


def test_wait_messages_description_mentions_terminal_work() -> None:
    description = WaitMessagesTool().description
    assert "terminal" in description
    assert "child agents" in description


# ── 重启后的失效告知 ─────────────────────────────────────────────────────


def _assistant_call(task_id: int, run_id: int, sequence: int, tool_name: str):
    return ConversationTaskContextRecord(
        task_id=task_id,
        run_id=run_id,
        message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"call-{sequence}",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": "{}"},
                }
            ],
        },
        include_in_context=True,
        sequence=sequence,
    )


class _Contexts:
    def __init__(self, calls: dict[int, list[tuple[int, str]]]) -> None:
        self._calls = calls

    def has_tool_call(
        self, task_id: int, tool_names: frozenset[str], *, run_id: int | None = None
    ) -> bool:
        return any(
            name in tool_names and (run_id is None or owner == run_id)
            for owner, name in self._calls.get(task_id, [])
        )


@pytest.fixture
def restart_env(monkeypatch: pytest.MonkeyPatch):
    """重启唤醒的协作者全用替身：Task 当前 Run、context 调用事实、space 与投递。"""
    from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

    runs = {
        1: SimpleNamespace(id=11, status="suspended"),  # 挂起且本 Run 调用过终端
        2: SimpleNamespace(id=21, status="suspended"),  # 挂起但终端调用在旧 Run
        3: SimpleNamespace(id=31, status="completed"),  # 已结束：交给首次驱动提醒
        4: None,
    }
    calls = {
        1: [(11, "terminal_write")],
        2: [(20, "terminal_start"), (21, "wait_messages")],
        3: [(31, "terminal_start")],
    }
    spaces: dict[int, SimpleNamespace] = {}
    delivered: list[tuple[int, str, dict[str, object]]] = []
    tasks = SimpleNamespace(require_current_run=lambda task_id: runs[task_id])
    monkeypatch.setattr(
        terminal_invalidation,
        "service_depends",
        SimpleNamespace(
            get_task_service=lambda: tasks,
            get_conversation_task_context_service=lambda: _Contexts(calls),
        ),
    )
    monkeypatch.setattr(
        terminal_invalidation,
        "_terminal_tool_names",
        lambda: frozenset({"terminal_start", "terminal_write", "terminal_read"}),
    )
    monkeypatch.setattr(
        task_runtime_spaces,
        "get_or_create",
        lambda task_id: spaces.setdefault(
            task_id,
            SimpleNamespace(task_id=task_id, terminals_invalidation_announced=False),
        ),
    )

    def deliver(task_id: int, *, tool_name: str, result: dict[str, object]) -> bool:
        delivered.append((task_id, tool_name, result))
        return True

    monkeypatch.setattr(terminal_invalidation, "deliver_background_result", deliver)
    return SimpleNamespace(spaces=spaces, delivered=delivered, task_ids=set(runs))


def test_restart_wakes_only_runs_suspended_on_terminals(restart_env) -> None:
    assert terminal_invalidation.wake_suspended_terminal_runs(restart_env.task_ids) == [1]
    [(task_id, tool_name, result)] = restart_env.delivered
    assert (task_id, tool_name) == (1, "terminal_wait")
    assert result["end_reason"] == "backend_restarted"
    assert restart_env.spaces[1].terminals_invalidation_announced is True


def test_woken_task_does_not_get_a_second_notice_on_first_drive(restart_env) -> None:
    terminal_invalidation.wake_suspended_terminal_runs(restart_env.task_ids)
    assert terminal_invalidation.first_drive_notice(restart_env.spaces[1]) is None


def test_first_drive_notice_once_per_process(restart_env) -> None:
    space = SimpleNamespace(task_id=3, terminals_invalidation_announced=False)
    message = terminal_invalidation.first_drive_notice(space)
    assert message is not None
    assert isinstance(message.body, NoticeBody)
    assert message.body.notice_kind == "terminals_invalidated"
    assert message.delivery == "immediate"
    assert terminal_invalidation.first_drive_notice(space) is None


def test_first_drive_without_terminal_start_marks_announced(restart_env) -> None:
    space = SimpleNamespace(task_id=4, terminals_invalidation_announced=False)
    assert terminal_invalidation.first_drive_notice(space) is None
    assert space.terminals_invalidation_announced is True


@pytest.mark.usefixtures("isolated_storage")
def test_has_tool_call_reads_in_context_assistant_calls(tmp_path: Path) -> None:
    workspace = depends.get_workspace_crud().create("ws", str(tmp_path))
    task = depends.get_task_crud().create(workspace.id, "task")
    runs = depends.get_conversation_run_crud()
    first = runs.create(task.id, "completed").id
    second = runs.create(task.id, "completed").id
    crud = depends.get_conversation_task_context_crud()
    crud.create(_assistant_call(task.id, first, 1, "terminal_start"))
    crud.create(_assistant_call(task.id, second, 2, "read_file"))
    service = depends.get_conversation_task_context_service()
    names = frozenset({"terminal_start"})
    assert service.has_tool_call(task.id, names)
    assert service.has_tool_call(task.id, names, run_id=first)
    assert not service.has_tool_call(task.id, names, run_id=second)
    assert not service.has_tool_call(task.id, frozenset({"terminal_close"}))
