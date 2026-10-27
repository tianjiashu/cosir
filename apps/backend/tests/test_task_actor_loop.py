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

    def create_run(self, task_id: int, launch: RunLaunch) -> SimpleNamespace:
        run = SimpleNamespace(
            id=len(self.runs) + 1,
            task_id=task_id,
            status="running",
            agent_id=launch.agent_id,
            model_config_id=launch.model_config_id,
            options=launch.options,
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
    for getter, fake in (
        ("get_conversation_run_ledger", ledger),
        ("get_task_service", ledger),
        ("get_task_message_queue", queue),
    ):
        monkeypatch.setattr(actor_module.service_depends, getter, lambda fake=fake: fake)
    space = SimpleNamespace(task_id=_TASK_ID, async_operation=contextlib.nullcontext)
    return SimpleNamespace(actor=TaskActor(space), queue=queue, ledger=ledger)


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
