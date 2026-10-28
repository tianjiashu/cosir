"""TaskActor 常驻 loop：监听外部队列，按 Run 状态投递、唤醒或开新 Run；随 space 物化拉起。

调度协作者（队列、Task 当前 Run、Run 台账、驱动）都是替身，只验证 actor 的推进与生命周期语义。
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from app.core import task_actor as actor_module
from app.core.task_actor import SessionConflictError, TaskActor
from app.core.workflow.inbox.task_message import (
    BackgroundResultBody,
    NoticeBody,
    RunLaunch,
    TaskMessage,
    UserInputBody,
)
from app.models.enums.conversation_run_status import RunTransition

_TASK_ID = 7


class _Queue:
    """按水位阻塞的外部队列替身；``put`` 唤醒等待方，``ack`` 由测试模拟 prepare_input 消费。"""

    def __init__(self) -> None:
        self.rows: list[TaskMessage] = []
        self._next_id = 1
        self._changed = asyncio.Event()

    def put(self, body: Any, delivery: str = "immediate") -> TaskMessage:
        message = TaskMessage.of(body, delivery=delivery, message_id=self._next_id)
        self._next_id += 1
        self.rows.append(message)
        self._changed.set()
        return message

    def ack(self, *ids: int) -> None:
        self.rows = [row for row in self.rows if row.id not in ids]

    async def pending(self, _task_id: int, after_id: int = 0) -> list[TaskMessage]:
        while not any(row.id > after_id for row in self.rows):
            self._changed.clear()
            await self._changed.wait()
        return list(self.rows)

    def pending_nowait(self, _task_id: int) -> list[TaskMessage]:
        return list(self.rows)


class _Ledger:
    """Run 台账与 Task 当前 Run 替身：建 Run 即成为当前 Run，迁移按来源状态条件生效。"""

    def __init__(self) -> None:
        self.runs: dict[int, SimpleNamespace] = {}
        self.current: SimpleNamespace | None = None
        self.created: list[SimpleNamespace] = []

    def create_run(
        self, task_id: int, launch: RunLaunch, trigger_tool_call_id: str | None = None
    ) -> SimpleNamespace:
        run = SimpleNamespace(
            id=len(self.runs) + 1,
            task_id=task_id,
            status="running",
            agent_id=launch.agent_id,
            model_config_id=launch.model_config_id,
            options=launch.options,
            trigger_tool_call_id=trigger_tool_call_id,
        )
        self.runs[run.id] = run
        self.current = run
        self.created.append(run)
        return run

    def transition(self, run_id: int, transition: RunTransition, **_kw: Any):
        run = self.runs[run_id]
        sources, target, _ = transition.value
        if run.status not in {source.value for source in sources}:
            return None
        run.status = target.value
        return run

    def get_run(self, run_id: int) -> SimpleNamespace:
        return self.runs[run_id]

    def require_current_run(self, _task_id: int) -> SimpleNamespace | None:
        return self.current

    def list_child_tasks(self, _task_id: int) -> list[SimpleNamespace]:
        return []


class _Driver:
    """驱动替身：取光 mailbox 并确认（模拟 prepare_input 消费），按测试给出的状态结束。"""

    drives: ClassVar[list[tuple[int, list[int | None]]]] = []
    finish: ClassVar[dict[int, asyncio.Event]] = {}
    settle: ClassVar[dict[int, str]] = {}
    ledger: _Ledger
    queue: _Queue

    def __init__(self, mailbox) -> None:
        self._mailbox = mailbox

    async def drive(self, run) -> None:
        drained = []
        while not self._mailbox.isEmpty():
            drained.append(self._mailbox.pop().id)
        _Driver.drives.append((run.id, drained))
        _Driver.queue.ack(*(message_id for message_id in drained if message_id is not None))
        event = _Driver.finish.setdefault(run.id, asyncio.Event())
        await event.wait()
        _Driver.ledger.runs[run.id].status = _Driver.settle.get(run.id, "completed")


def _user(text: str = "hi") -> UserInputBody:
    return UserInputBody(display_text=text, input_text=text, launch=RunLaunch(model_config_id=1))


def _notice() -> NoticeBody:
    return NoticeBody(content="config changed", notice_kind="probe")


def _result() -> BackgroundResultBody:
    return BackgroundResultBody(tool_name="probe", tool_result="done")


@pytest.fixture
def harness(monkeypatch):
    queue, ledger = _Queue(), _Ledger()
    _Driver.drives, _Driver.finish, _Driver.settle = [], {}, {}
    _Driver.ledger, _Driver.queue = ledger, queue
    monkeypatch.setattr(actor_module, "RunDriver", _Driver)
    monkeypatch.setattr(actor_module, "_republish", lambda _task_id: None)
    refreshed: list[list[int | None]] = []
    # 记下每次刷新排队投影时队列里已有的消息，用来断言「先刷新、再投递」。
    monkeypatch.setattr(
        actor_module,
        "_refresh_queued",
        lambda _task_id: refreshed.append([message.id for message in queue.pending_nowait(_TASK_ID)]),
    )
    for getter, fake in (
        ("get_conversation_run_ledger", ledger),
        ("get_task_service", ledger),
        ("get_task_message_queue", queue),
    ):
        monkeypatch.setattr(actor_module.service_depends, getter, lambda fake=fake: fake)
    # 撤销时的占位结算写库；记下调用，不触达存储（占位结算见 test_deferred_placeholders）。
    settled: list[tuple[int, list[str]]] = []
    monkeypatch.setattr(
        actor_module.placeholders,
        "settle_placeholders",
        lambda task_id, ids, _status, _hint: settled.append((task_id, list(ids))) or len(ids),
    )
    # 已告知旧终端失效：本文件不验证首次驱动提醒（见 test_restart_settlement）。
    space = SimpleNamespace(
        task_id=_TASK_ID,
        async_operation=contextlib.nullcontext,
        terminals_invalidation_announced=True,
    )
    return SimpleNamespace(
        actor=TaskActor(space), queue=queue, ledger=ledger, refreshed=refreshed, settled=settled
    )


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def _finish(run_id: int, status: str = "completed") -> None:
    _Driver.settle[run_id] = status
    _Driver.finish.setdefault(run_id, asyncio.Event()).set()
    await _settle()


async def test_new_message_on_idle_task_starts_a_run_with_all_pending(harness) -> None:
    harness.actor.start()
    notice = harness.queue.put(_notice())
    user = harness.queue.put(_user())
    await _settle()

    [run] = harness.ledger.created
    assert _Driver.drives == [(run.id, [notice.id, user.id])]
    await harness.actor.abort()


async def test_system_notice_alone_does_not_start_a_run(harness) -> None:
    harness.actor.start()
    harness.queue.put(_notice())
    await _settle()

    assert harness.ledger.created == []
    await harness.actor.abort()


async def test_messages_arriving_while_running_are_posted_not_restarted(harness) -> None:
    harness.actor.start()
    first = harness.queue.put(_user("first"))
    await _settle()
    harness.queue.ack(first.id)
    later = harness.queue.put(_user("later"))
    queued = harness.queue.put(_user("next"), delivery="next_run")
    await _settle()

    assert len(harness.ledger.created) == 1
    # 运行中到达的 immediate 消息投进 mailbox，next_run 留给下一个 Run。
    assert harness.actor.mailbox.pop().id == later.id
    assert harness.actor.mailbox.isEmpty()
    assert queued in harness.queue.rows
    await harness.actor.abort()


async def test_completed_run_continues_with_leftover_messages(harness) -> None:
    harness.actor.start()
    first = harness.queue.put(_user("first"))
    await _settle()
    harness.queue.ack(first.id)
    leftover = harness.queue.put(_user("later"), delivery="next_run")
    await _settle()
    [run] = harness.ledger.created

    await _finish(run.id, "completed")

    second = harness.ledger.created[-1]
    assert second.id != run.id
    assert _Driver.drives[-1] == (second.id, [leftover.id])
    await harness.actor.abort()


async def test_cancelled_run_does_not_continue_until_a_new_message(harness) -> None:
    harness.actor.start()
    first = harness.queue.put(_user("first"))
    await _settle()
    harness.queue.ack(first.id)
    harness.queue.put(_user("later"), delivery="next_run")
    await _settle()
    [run] = harness.ledger.created

    await _finish(run.id, "cancelled")
    assert len(harness.ledger.created) == 1

    # 「有消息就处理」：停止后新到达的后台结果照样开出新 Run，残留消息一并投递。
    harness.queue.put(_result())
    await _settle()
    assert len(harness.ledger.created) == 2
    await harness.actor.abort()


async def test_suspended_run_is_woken_by_new_message_not_by_notice(harness) -> None:
    run = harness.ledger.create_run(_TASK_ID, RunLaunch(model_config_id=1))
    run.status = "suspended"
    harness.ledger.created.clear()
    harness.actor.start()

    notice = harness.queue.put(_notice())
    await _settle()
    assert run.status == "suspended"

    result = harness.queue.put(_result())
    await _settle()
    assert run.status == "running"
    # 通知不单独唤醒，但唤醒后随 immediate 消息一并投递。
    assert _Driver.drives == [(run.id, [notice.id, result.id])]
    await harness.actor.abort()


async def test_resume_starts_the_stopped_run_and_rejects_while_running(harness) -> None:
    run = harness.ledger.create_run(_TASK_ID, RunLaunch(model_config_id=1))
    run.status = "cancelled"

    await harness.actor.resume()
    assert run.status == "running"
    with pytest.raises(SessionConflictError):
        await harness.actor.resume()
    await harness.actor.abort()


async def test_stop_waits_for_the_execution_then_stops_the_loop(harness, monkeypatch) -> None:
    cancelled: list[tuple[int, str]] = []
    monkeypatch.setattr(
        actor_module.cancellation_registry,
        "request_cancel",
        lambda task_id, reason="user": cancelled.append((task_id, reason)),
    )
    harness.actor.start()
    harness.queue.put(_user())
    await _settle()
    [run] = harness.ledger.created

    stopping = asyncio.create_task(harness.actor.stop())
    await _settle()
    assert not stopping.done()
    assert cancelled == [(_TASK_ID, "parent")]

    await _finish(run.id, "cancelled")
    await asyncio.wait_for(stopping, 1)
    assert harness.actor.stopped.is_set()
    # 停下后不再接续，也不再响应新消息。
    harness.queue.put(_user("after stop"))
    await _settle()
    assert len(harness.ledger.created) == 1


async def test_stop_still_wakes_state_waiters_when_the_execution_settles(harness) -> None:
    """删除前停止走「不接续」的早退分支，也必须发状态通知：否则 ``wait_idle`` 只能等到超时。"""
    harness.actor.start()
    harness.queue.put(_user())
    await _settle()
    [run] = harness.ledger.created
    changed = harness.actor.state_changed

    stopping = asyncio.create_task(harness.actor.stop())
    await _settle()
    await _finish(run.id, "cancelled")
    await asyncio.wait_for(stopping, 1)

    assert changed.is_set()


async def test_queued_projection_is_refreshed_before_a_running_delivery(harness) -> None:
    """运行中到达的消息先进排队投影再进 mailbox：消费投影只搬移排队条目，缺了就丢消息。"""
    harness.actor.start()
    harness.queue.put(_user("first"))
    await _settle()
    [run] = harness.ledger.created
    posted: list[int | None] = []
    original_post = harness.actor.mailbox.post

    def post(message: TaskMessage) -> None:
        posted.append(message.id)
        assert any(message.id in ids for ids in harness.refreshed)
        original_post(message)

    harness.actor.mailbox.post = post
    second = harness.queue.put(_user("second"))
    await _settle()

    assert posted == [second.id]
    await _finish(run.id)
    await harness.actor.abort()


async def test_wait_idle_turns_true_after_the_run_completes(harness) -> None:
    harness.actor.start()
    harness.queue.put(_user())
    await _settle()
    [run] = harness.ledger.created
    assert await harness.actor.wait_idle(0.01) is False

    await _finish(run.id, "completed")
    assert await harness.actor.wait_idle(1) is True
    await harness.actor.abort()


# ---------------------------------------------------------------------------
# 注册表：space 物化即拉起 actor，入队前确保 space 存在
# ---------------------------------------------------------------------------


class _FakeActor:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.aborted = 0

    def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def abort(self) -> None:
        self.aborted += 1


class _FakeSpace:
    def __init__(self, task_id: int) -> None:
        self.task_id = task_id
        self.actor = _FakeActor()


@pytest.fixture
def registry(monkeypatch):
    from app.task_runtime import task_runtime_space_registry as registry_module

    monkeypatch.setattr(registry_module, "TaskRuntimeSpace", _FakeSpace)
    return registry_module.TaskRuntimeSpaceRegistry()


async def test_spaces_materialized_before_bind_start_on_bind(registry) -> None:
    space = registry.get_or_create(1)
    await _settle()
    assert space.actor.started == 0

    registry.bind_loop(asyncio.get_running_loop())
    await _settle()
    assert space.actor.started == 1


async def test_space_materialized_in_a_worker_thread_starts_on_the_loop(registry) -> None:
    registry.bind_loop(asyncio.get_running_loop())
    space = await asyncio.to_thread(registry.get_or_create, 2)
    await _settle()
    assert space.actor.started == 1
    assert registry.get_or_create(2) is space
    await _settle()
    assert space.actor.started == 1


async def test_enqueue_materializes_the_space_before_putting(registry, monkeypatch) -> None:
    from app.service import depends

    puts: list[tuple[int, Any]] = []
    queue = SimpleNamespace(
        put=lambda task_id, message, session=None: puts.append((task_id, message)) or 9
    )
    monkeypatch.setattr(depends, "get_task_message_queue", lambda: queue)
    registry.bind_loop(asyncio.get_running_loop())

    message = TaskMessage.of(_result())
    assert registry.enqueue(3, message) == 9
    await _settle()
    assert registry.get(3).actor.started == 1
    assert puts == [(3, message)]


async def test_stop_only_touches_materialized_spaces_and_aclose_aborts_all(registry) -> None:
    first, second = registry.get_or_create(1), registry.get_or_create(2)

    await registry.stop([1, 99])
    assert (first.actor.stopped, second.actor.stopped) == (1, 0)

    await registry.aclose()
    assert (first.actor.aborted, second.actor.aborted) == (1, 1)
    assert registry.existing_spaces() == ()


# ── 延迟结果与首次驱动提醒 ─────────────────────────────────────────────────


async def test_cancelling_suspended_run_drops_deferred_results(harness) -> None:
    """Run 取消即撤销本 Task 的后台等待：撤销回调被调用，之后的完成返回 False、不再投递。"""
    from app.core.workflow.deferred.deferred_results import deferred_results

    run = harness.ledger.create_run(_TASK_ID, RunLaunch(model_config_id=1))
    run.status = "suspended"
    cancelled: list[str] = []
    deferred_results.register(
        "call-t1",
        task_id=_TASK_ID,
        run_id=run.id,
        kind="terminal_wait",
        label="terminal:t1",
        on_cancel=lambda: cancelled.append("t1"),
    )
    deferred_results.register(
        "call-x",
        task_id=_TASK_ID + 1,
        run_id=None,
        kind="terminal_wait",
        label="x",
        on_cancel=lambda: None,
    )
    try:
        await harness.actor.cancel("parent")
        assert run.status == "cancelled"
        assert cancelled == ["t1"]
        # 被撤销的后台条目随即把占位行推到 cancelled。
        assert harness.settled == [(_TASK_ID, ["call-t1"])]
        assert deferred_results.pending(_TASK_ID) == []
        assert deferred_results.complete("call-t1", {}) is False
        # 其他 Task 的等待不受影响。
        assert deferred_results.pending(_TASK_ID + 1) == ["x"]
    finally:
        deferred_results.drop_task(_TASK_ID + 1)
        assert deferred_results.complete("call-x", {}) is False


async def test_running_run_cancel_drops_watches_when_execution_settles(harness) -> None:
    from app.core.workflow.deferred.deferred_results import deferred_results

    harness.actor.start()
    harness.queue.put(_user())
    await _settle()
    [run] = harness.ledger.created
    dropped: list[int] = []
    signalled_at_drop: list[bool] = []

    def on_cancel() -> None:
        # 撤销必须先于取走取消信号：取走之后到达的结论不再被丢弃，会开出新 Run。
        signalled_at_drop.append(actor_module.cancellation_registry.is_cancelled(_TASK_ID))
        dropped.append(run.id)

    deferred_results.register(
        "call-t2",
        task_id=_TASK_ID,
        run_id=run.id,
        kind="terminal_wait",
        label="terminal:t2",
        on_cancel=on_cancel,
    )
    await harness.actor.cancel("user")
    await _finish(run.id, "cancelled")
    assert dropped == [run.id]
    assert signalled_at_drop == [True]
    assert deferred_results.pending(_TASK_ID) == []
    await harness.actor.abort()


async def test_completed_run_keeps_deferred_results(harness) -> None:
    """完成或挂起不撤销：后台等待的结论稍后照常投递、开新 Run 或唤醒。"""
    from app.core.workflow.deferred.deferred_results import deferred_results

    harness.actor.start()
    harness.queue.put(_user())
    await _settle()
    [run] = harness.ledger.created
    deferred_results.register(
        "call-t3",
        task_id=_TASK_ID,
        run_id=run.id,
        kind="terminal_wait",
        label="terminal:t3",
        on_cancel=lambda: None,
    )
    await _finish(run.id, "suspended")
    assert deferred_results.pending(_TASK_ID) == ["terminal:t3"]
    deferred_results.drop_task(_TASK_ID)
    await harness.actor.abort()


async def test_first_drive_notice_is_posted_into_the_run_mailbox(harness, monkeypatch) -> None:
    notice = TaskMessage.of(
        NoticeBody(content="terminals gone", notice_kind="terminals_invalidated"), message_id=None
    )
    calls: list[object] = []

    def first_drive_notice(space):
        calls.append(space)
        return notice if len(calls) == 1 else None

    monkeypatch.setattr(actor_module.restart_settlement, "first_drive_notice", first_drive_notice)
    posted: list[TaskMessage] = []
    original_post = harness.actor.mailbox.post

    def record(message: TaskMessage) -> None:
        posted.append(message)
        original_post(message)

    monkeypatch.setattr(harness.actor.mailbox, "post", record)
    harness.actor.start()
    user = harness.queue.put(_user())
    await _settle()
    [run] = harness.ledger.created
    assert notice in posted
    assert _Driver.drives == [(run.id, [user.id, None])] or _Driver.drives == [
        (run.id, [None, user.id])
    ]
    await _finish(run.id)
    harness.queue.put(_user("again"))
    await _settle()
    assert len(calls) == 2
    assert posted.count(notice) == 1
    await harness.actor.abort()
