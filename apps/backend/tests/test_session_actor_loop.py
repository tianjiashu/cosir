"""SessionActor 常驻 loop：监听外部队列，按 Run 状态投递、唤醒或开新 Run；随 space 物化拉起。

调度协作者（队列、Session 当前 Run、Run 台账、驱动）都是替身，只验证 actor 的推进与生命周期语义。
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from app.core import session_actor as actor_module
from app.core import session_execution as execution_module
from app.core import session_recall
from app.core.events import SessionQueueChangedEvent
from app.core.events.event_sink import set_event_sink
from app.core.session_actor import SessionActor
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.core.workflow.message_queue.session_message import (
    BackgroundResultBody,
    NoticeBody,
    RunLaunch,
    SessionMessage,
    UserInputBody,
)
from app.models.conversation_input import UserInputPresentation
from app.models.enums.run_status import RunTransition
from app.models.errors.session_command_errors import SessionCommandConflictError
from app.tools.tool_handler.terminal.command_job import CommandConclusion
from tests.command_job_fakes import FakeCommandJob

_SESSION_ID = 7


class _Queue:
    """按水位阻塞的外部队列替身；``put`` 唤醒等待方，``ack`` 由测试模拟 prepare_input 消费。"""

    def __init__(self) -> None:
        self.rows: list[SessionMessage] = []
        self._next_id = 1
        self._changed = asyncio.Event()

    def put(self, body: Any, delivery: str = "immediate") -> SessionMessage:
        message = SessionMessage.of(body, delivery=delivery, message_id=self._next_id)
        self._next_id += 1
        self.rows.append(message)
        self._changed.set()
        return message

    def ack(self, *ids: int) -> None:
        self.rows = [row for row in self.rows if row.id not in ids]

    async def pending(self, _session_id: int, after_id: int = 0) -> list[SessionMessage]:
        while not any(row.id > after_id for row in self.rows):
            self._changed.clear()
            await self._changed.wait()
        return list(self.rows)

    def pending_nowait(self, _session_id: int) -> list[SessionMessage]:
        return list(self.rows)


class _Ledger:
    """Run 台账与 Session 当前 Run 替身：建 Run 即成为当前 Run，迁移按来源状态条件生效。"""

    def __init__(self) -> None:
        self.runs: dict[int, SimpleNamespace] = {}
        self.current: SimpleNamespace | None = None
        self.created: list[SimpleNamespace] = []
        self.fail_create = False

    def create_run(self, session_id: int, launch: RunLaunch) -> SimpleNamespace:
        if self.fail_create:
            raise RuntimeError("storage unavailable")
        run = SimpleNamespace(
            id=len(self.runs) + 1,
            session_id=session_id,
            status="running",
            agent_id=launch.agent_id,
            model_config_id=launch.model_config_id,
            reasoning_effort=launch.reasoning_effort,
            end_reason=None,
        )
        self.runs[run.id] = run
        self.current = run
        self.created.append(run)
        return run

    def transition(self, run_id: int, transition: RunTransition, **kw: Any):
        run = self.runs[run_id]
        sources, target, _ = transition.value
        if run.status not in {source.value for source in sources}:
            return None
        run.status = target.value
        run.end_reason = kw.get("end_reason")
        return run

    def get_run(self, run_id: int) -> SimpleNamespace:
        return self.runs[run_id]

    def require_current_run(self, _session_id: int) -> SimpleNamespace | None:
        return self.current

    def list_derived_sessions(self, _session_id: int) -> list[SimpleNamespace]:
        return []


class _Driver:
    """驱动替身：取光 mailbox 并确认（模拟 prepare_input 消费），按测试给出的状态结束。"""

    drives: ClassVar[list[tuple[int, list[int | None]]]] = []
    finish: ClassVar[dict[int, asyncio.Event]] = {}
    settle: ClassVar[dict[int, str]] = {}
    fail: ClassVar[set[int]] = set()
    ledger: _Ledger
    queue: _Queue
    # 本次执行的 mailbox 归 space 持有（``RunDriver`` 不再接收它），由 fixture 注入。
    mailbox: ClassVar[RunMailbox] = RunMailbox()

    def __init__(self, *_args: object) -> None:
        self._mailbox = _Driver.mailbox

    async def drive(self, run, preceding_run) -> None:
        if run.id in _Driver.fail:
            raise RuntimeError("run settlement failed")
        drained = []
        while not self._mailbox.isEmpty():
            drained.append(self._mailbox.pop().id)
        _Driver.drives.append((run.id, drained))
        _Driver.queue.ack(*(message_id for message_id in drained if message_id is not None))
        event = _Driver.finish.setdefault(run.id, asyncio.Event())
        await event.wait()
        _Driver.ledger.runs[run.id].status = _Driver.settle.get(run.id, "completed")


def _user(text: str = "hi") -> UserInputBody:
    return UserInputBody(
        text=text,
        launch=RunLaunch(model_config_id="test/m1"),
        presentation=UserInputPresentation(text=text),
    )


def _notice() -> NoticeBody:
    return NoticeBody(content="config changed", notice_kind="system_prompt_delta")


def _result() -> BackgroundResultBody:
    return BackgroundResultBody(
        ended_at="2026-01-01T00:01:00+00:00", tool_call_id="call-probe",
        tool_name="execute_terminal", tool_result="done",
    )


@pytest.fixture
def harness(monkeypatch):
    queue, ledger = _Queue(), _Ledger()
    _Driver.drives, _Driver.finish, _Driver.settle, _Driver.fail = [], {}, {}, set()
    _Driver.ledger, _Driver.queue = ledger, queue
    monkeypatch.setattr(execution_module, "RunDriver", _Driver)
    # 恢复前查最后一个回合是否已撤回；本文件不撤回（见 test_transport_command_chain 的撤回用例）。
    monkeypatch.setattr(
        actor_module,
        "ContextStore",
        lambda: SimpleNamespace(run_last_turn_withdrawn=lambda _session_id, _run_id: False),
    )
    refreshed: list[list[int | None]] = []

    # 记下每次交出「队列变了」时队列里已有的消息，用来断言「先刷新排队投影、再投递」。
    def sink(event: object) -> None:
        if isinstance(event, SessionQueueChangedEvent):
            refreshed.append([message.id for message in queue.pending_nowait(_SESSION_ID)])

    set_event_sink(sink)  # 下一个用例由 conftest 的 autouse 夹具换回
    # 模型配置文件替身：开新 Run 前 actor 查模型是否还在，列在 ``missing`` 里的视为已删除。
    missing: set[str] = set()
    models = SimpleNamespace(exists=lambda model_config_id: model_config_id not in missing)
    for getter, fake in (
        ("get_model_config_service", models),
        ("get_run_ledger", ledger),
        ("get_session_service", ledger),
        ("get_session_message_queue", queue),
    ):
        monkeypatch.setattr(actor_module.service_depends, getter, lambda fake=fake: fake)
    # 撤销时的占位结算写库；记下调用，不触达存储（占位结算见 test_background_placeholders）。
    settled: list[tuple[int, list[str]]] = []
    monkeypatch.setattr(
        session_recall,
        "cancel_placeholders",
        lambda session_id, ids, _hint, **_kw: settled.append((session_id, list(ids)))
        or frozenset({session_id}),
    )
    # Run 落定时作废等用户回应的决定要读库；记下调用，``expiring`` 里的调用视为被作废
    # （结算本身见 test_decision_expiry_after_run）。
    expiring: list[str] = []
    expired: list[tuple[int, str]] = []
    monkeypatch.setattr(
        actor_module,
        "expire_decisions_after_run",
        lambda session_id, hint: expired.append((session_id, hint)) or list(expiring),
    )
    space = SimpleNamespace(
        session_id=_SESSION_ID,
        async_operation=contextlib.nullcontext,
        mailbox=RunMailbox(),
    )
    actor = SessionActor(space)
    _Driver.mailbox = actor.mailbox
    return SimpleNamespace(
        actor=actor,
        queue=queue,
        ledger=ledger,
        refreshed=refreshed,
        settled=settled,
        missing_models=missing,
        expiring=expiring,
        expired=expired,
    )


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def _finish(run_id: int, status: str = "completed") -> None:
    _Driver.settle[run_id] = status
    _Driver.finish.setdefault(run_id, asyncio.Event()).set()
    await _settle()


async def test_new_message_on_idle_session_starts_a_run_with_all_pending(harness) -> None:
    harness.actor.start()
    notice = harness.queue.put(_notice())
    user = harness.queue.put(_user())
    await _settle()

    [run] = harness.ledger.created
    assert _Driver.drives == [(run.id, [notice.id, user.id])]
    await harness.actor.abort()


async def test_input_whose_model_was_deleted_stays_queued_until_a_new_input(harness) -> None:
    """模型从配置文件删掉后 Run 仍留着原 id：开不了新 Run 就不动，消息留在队列里。"""

    harness.missing_models.add(_user().launch.model_config_id)
    harness.actor.start()
    harness.queue.put(_user("old model"))
    await _settle()

    assert harness.ledger.created == []
    harness.missing_models.clear()
    harness.queue.put(_user("new model"))
    await _settle()
    assert len(harness.ledger.created) == 1
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


async def test_request_cancel_from_a_worker_thread_runs_cancel_on_the_actor_loop(
    harness, monkeypatch
) -> None:
    calls: list[tuple[str, asyncio.AbstractEventLoop]] = []

    async def cancel(reason: str) -> None:
        calls.append((reason, asyncio.get_running_loop()))

    monkeypatch.setattr(harness.actor, "cancel", cancel)
    harness.actor.start()

    await asyncio.to_thread(harness.actor.request_cancel, "parent")
    await _settle()

    assert calls == [("parent", asyncio.get_running_loop())]
    await harness.actor.abort()


async def test_request_cancel_before_start_is_dropped_without_raising(harness, monkeypatch) -> None:
    calls: list[str] = []

    async def cancel(reason: str) -> None:
        calls.append(reason)

    monkeypatch.setattr(harness.actor, "cancel", cancel)

    harness.actor.request_cancel("parent")
    await _settle()

    assert calls == []


async def test_suspended_run_is_woken_by_new_message_not_by_notice(harness) -> None:
    run = harness.ledger.create_run(_SESSION_ID, RunLaunch(model_config_id="test/m1"))
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
    run = harness.ledger.create_run(_SESSION_ID, RunLaunch(model_config_id="test/m1"))
    run.status = "cancelled"

    await harness.actor.resume()
    assert run.status == "running"
    with pytest.raises(SessionCommandConflictError):
        await harness.actor.resume()
    await harness.actor.abort()


async def test_resume_is_rejected_after_stop_for_deletion(harness) -> None:
    """删除前的停止是墓碑：恢复是唯一不经 loop 开出执行的入口，停止之后不得再放行。"""
    run = harness.ledger.create_run(_SESSION_ID, RunLaunch(model_config_id="test/m1"))
    run.status = "cancelled"

    await harness.actor.stop()

    with pytest.raises(SessionCommandConflictError):
        await harness.actor.resume()
    assert run.status == "cancelled"


async def test_stop_waits_for_the_execution_then_stops_the_loop(harness, monkeypatch) -> None:
    cancelled: list[tuple[int, str]] = []
    monkeypatch.setattr(
        execution_module.cancellation_registry,
        "request_cancel",
        lambda session_id, reason="user": cancelled.append((session_id, reason)),
    )
    harness.actor.start()
    harness.queue.put(_user())
    await _settle()
    [run] = harness.ledger.created

    stopping = asyncio.create_task(harness.actor.stop())
    await _settle()
    assert not stopping.done()
    assert cancelled == [(_SESSION_ID, "parent")]

    await _finish(run.id, "cancelled")
    await asyncio.wait_for(stopping, 1)
    assert harness.actor.stopped.is_set()
    # 停下后不再接续，也不再响应新消息。
    harness.queue.put(_user("after stop"))
    await _settle()
    assert len(harness.ledger.created) == 1


async def test_queued_projection_is_refreshed_before_a_running_delivery(harness) -> None:
    """运行中到达的消息先进排队投影再进 mailbox：消费投影只搬移排队条目，缺了就丢消息。"""
    harness.actor.start()
    harness.queue.put(_user("first"))
    await _settle()
    [run] = harness.ledger.created
    posted: list[int | None] = []
    original_post = harness.actor.mailbox.post

    def post(message: SessionMessage) -> None:
        posted.append(message.id)
        assert any(message.id in ids for ids in harness.refreshed)
        original_post(message)

    harness.actor.mailbox.post = post
    second = harness.queue.put(_user("second"))
    await _settle()

    assert posted == [second.id]
    await _finish(run.id)
    await harness.actor.abort()


async def test_phase_turns_idle_after_the_run_completes(harness) -> None:
    harness.actor.start()
    harness.queue.put(_user())
    await _settle()
    [run] = harness.ledger.created
    assert await harness.actor.phase() == "driving"

    await _finish(run.id, "completed")
    assert await harness.actor.phase() == "idle"
    await harness.actor.abort()


# ---------------------------------------------------------------------------
# 调度器失败：读队列失败不重试，落成明确、可观察的停止态
# ---------------------------------------------------------------------------


async def test_queue_read_failure_halts_the_loop_instead_of_killing_it_silently(harness) -> None:
    """读队列失败（坏行/库不可用）记录后进入停止态；之后不再调度任何 Run。"""
    actor = harness.actor
    healthy_pending = harness.queue.pending

    async def broken(_session_id: int, _after_id: int = 0) -> list[SessionMessage]:
        raise ValueError("bad row")

    harness.queue.pending = broken
    actor.start()
    await _settle()

    assert actor.stopped.is_set()
    # 停止态不自动恢复：队列恢复可用、再来一条消息，也不会开出 Run。
    harness.queue.pending = healthy_pending
    harness.queue.put(_user())
    await _settle()
    assert harness.ledger.created == []


async def test_drive_failure_still_releases_the_execution_and_takes_the_signal(harness) -> None:
    """驱动抛异常也要走释放边界：执行注销、取消信号取走，不残留到下一次执行。"""
    harness.actor.start()
    harness.queue.put(_user())
    await _settle()
    [run] = harness.ledger.created
    assert harness.actor.driving

    execution_module.cancellation_registry.request_cancel(_SESSION_ID, "user")
    _Driver.fail.add(run.id)
    _Driver.finish.setdefault(run.id, asyncio.Event()).set()
    await _settle()

    assert not harness.actor.driving
    # 信号残留会让同一 Session 的下一次执行一启动就被取消。
    assert execution_module.cancellation_registry.is_cancelled(_SESSION_ID) is False
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
    def __init__(self, session_id: int) -> None:
        self.session_id = session_id
        self.actor = _FakeActor()


@pytest.fixture
def registry(monkeypatch):
    from app.session_runtime import session_runtime_space_registry as registry_module

    monkeypatch.setattr(registry_module, "SessionRuntimeSpace", _FakeSpace)
    return registry_module.SessionRuntimeSpaceRegistry()


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
        put=lambda session_id, message, db=None: puts.append((session_id, message)) or 9
    )
    monkeypatch.setattr(depends, "get_session_message_queue", lambda: queue)
    registry.bind_loop(asyncio.get_running_loop())

    message = SessionMessage.of(_result())
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


# ── 后台结果与首次驱动提醒 ─────────────────────────────────────────────────


async def test_recalling_a_suspended_run_drops_background_entries(harness) -> None:
    """上游收回即撤销本 Session 的后台等待：撤销回调被调用，之后的完成返回 False、不再投递。"""
    from app.tools.tool_handler.terminal.command_results import command_results

    run = harness.ledger.create_run(_SESSION_ID, RunLaunch(model_config_id="test/m1"))
    run.status = "suspended"
    cancelled: list[str] = []
    command_results.register(
        "call-t1",
        session_id=_SESSION_ID,
        run_id=run.id,
        label="terminal:t1",
        job=FakeCommandJob(lambda: cancelled.append("t1")),
    )
    command_results.register(
        "call-x",
        session_id=_SESSION_ID + 1,
        run_id=None,
        label="x",
        job=FakeCommandJob(),
    )
    try:
        await harness.actor.cancel("parent")
        assert run.status == "cancelled"
        assert cancelled == ["t1"]
        # 被撤销的后台条目随即把占位行推到 cancelled。
        assert harness.settled == [(_SESSION_ID, ["call-t1"])]
        assert command_results.pending(_SESSION_ID) == []
        assert command_results.complete(_SESSION_ID, "call-t1", CommandConclusion({})) is False
        # 其他 Session 的等待不受影响。
        assert set(command_results.live_background(_SESSION_ID + 1)) == {"call-x"}
    finally:
        command_results.drop_session(_SESSION_ID + 1)
        assert command_results.complete(_SESSION_ID, "call-x", CommandConclusion({})) is False


async def test_user_stop_keeps_background_work_in_every_phase(harness) -> None:
    """用户停止只打断当前驱动：挂起、运行中与空闲时停止都不撤销后台工作，挂起与运行中的 Run 照常取消。"""
    from app.tools.tool_handler.terminal.command_results import command_results

    dropped: list[str] = []
    command_results.register(
        "call-keep", session_id=_SESSION_ID, run_id=None, label="keep",
        job=FakeCommandJob(lambda: dropped.append("cancel")),
    )
    try:
        suspended = harness.ledger.create_run(_SESSION_ID, RunLaunch(model_config_id="test/m1"))
        suspended.status = "suspended"
        await harness.actor.cancel("user")
        assert suspended.status == "cancelled"

        harness.actor.start()
        harness.queue.put(_user())
        await _settle()
        running = harness.ledger.created[-1]
        await harness.actor.cancel("user")
        await _finish(running.id, "cancelled")

        await harness.actor.cancel("user")
        assert dropped == []
        assert harness.settled == []
        assert command_results.pending(_SESSION_ID) == ["keep"]
    finally:
        command_results.drop_session(_SESSION_ID)
        await harness.actor.abort()


async def test_recalling_a_running_run_drops_work_after_the_signal(harness) -> None:
    """收回运行中的 Run：先打信号再撤销，信号在时这次驱动剩下的登记被拒收，不会留下后台工作。"""
    from app.tools.tool_handler.terminal.command_results import command_results

    harness.actor.start()
    harness.queue.put(_user())
    await _settle()
    [run] = harness.ledger.created
    dropped: list[int] = []
    signalled_at_drop: list[bool] = []

    def on_cancel() -> None:
        signalled_at_drop.append(execution_module.cancellation_registry.is_cancelled(_SESSION_ID))
        dropped.append(run.id)

    command_results.register(
        "call-t2",
        session_id=_SESSION_ID,
        run_id=run.id,
        label="terminal:t2",
        job=FakeCommandJob(on_cancel),
    )
    await harness.actor.cancel("parent")
    assert dropped == [run.id]
    assert signalled_at_drop == [True]
    late = FakeCommandJob()
    assert command_results.register(
        "call-late", session_id=_SESSION_ID, run_id=run.id, label="late", job=late,
    ) is None
    assert late.cancels == 1
    assert not command_results.has_work(_SESSION_ID)
    # 收回时先结算已写入的行；同一批的占位行要等驱动写完，驱动释放后再结算一次。
    assert harness.settled == [(_SESSION_ID, ["call-t2"])]
    await _finish(run.id, "cancelled")
    assert harness.settled == [(_SESSION_ID, ["call-t2"]), (_SESSION_ID, ["call-t2"])]
    await harness.actor.abort()


async def test_recalling_an_idle_session_drops_its_background_work(harness) -> None:
    """Run 已答复、Session 空闲：上游收回照样撤销后台工作，答复过的 Run 不改结局。"""
    from app.tools.tool_handler.terminal.command_results import command_results

    harness.actor.start()
    harness.queue.put(_user())
    await _settle()
    [run] = harness.ledger.created
    dropped: list[str] = []
    command_results.register(
        "call-idle", session_id=_SESSION_ID, run_id=run.id, label="idle",
        job=FakeCommandJob(lambda: dropped.append("parent")),
    )
    try:
        await _finish(run.id, "completed")
        await harness.actor.cancel("parent")
        assert dropped == ["parent"]
        assert not command_results.has_work(_SESSION_ID)
        assert harness.settled == [(_SESSION_ID, ["call-idle"])]
        assert run.status == "completed"
    finally:
        command_results.drop_session(_SESSION_ID)
        await harness.actor.abort()


async def test_completed_run_keeps_background_entries(harness) -> None:
    """完成或挂起不撤销：后台等待的结论稍后照常投递、开新 Run 或唤醒。"""
    from app.tools.tool_handler.terminal.command_results import command_results

    harness.actor.start()
    harness.queue.put(_user())
    await _settle()
    [run] = harness.ledger.created
    command_results.register(
        "call-t3",
        session_id=_SESSION_ID,
        run_id=run.id,
        label="terminal:t3",
        job=FakeCommandJob(),
    )
    await _finish(run.id, "suspended")
    assert command_results.pending(_SESSION_ID) == ["terminal:t3"]
    command_results.drop_session(_SESSION_ID)
    await harness.actor.abort()


# ---------------------------------------------------------------------------
# Run 落定：等用户回应的决定（提问）一并作废
# ---------------------------------------------------------------------------


async def test_stopping_a_run_suspended_on_a_question_expires_it_and_tells_the_model(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提问挂着时用户停止：卡片收敛为「已停止」，模型在下一次驱动得知这次提问没等到回答。

    不作废的话卡片还能答，回答会开新 Run、落进新回合，被停下的 Run 还会先读到「用户已停止」。
    """
    notices: list[tuple[int, SessionMessage]] = []
    monkeypatch.setattr(
        harness.queue, "put", lambda session_id, message: notices.append((session_id, message))
    )
    harness.expiring.append("call-ask")
    run = harness.ledger.create_run(_SESSION_ID, RunLaunch(model_config_id="test/m1"))
    run.status = "suspended"

    await harness.actor.cancel("user")

    assert run.status == "cancelled"
    assert harness.expired == [(_SESSION_ID, "已停止")]
    [(session_id, notice)] = notices
    assert session_id == _SESSION_ID and notice.kind == "system_notice"
    assert "tool_call_id=call-ask" in notice.body.content
    assert "stopped you instead of answering" in notice.body.content
    # 通知不开新 Run：恢复或下一条消息到来时才并入。
    assert notice.wakes(None) is False


@pytest.mark.parametrize(
    ("status", "expected"),
    [("failed", [(_SESSION_ID, "未作答")]), ("completed", []), ("suspended", [])],
)
async def test_only_a_run_that_fails_or_is_stopped_expires_its_questions(
    harness, status: str, expected: list[tuple[int, str]]
) -> None:
    """失败的 Run 收尾提问，标「未作答」。正常完成不作废：提问挂着时被别的消息唤醒、说完话就完成是
    正常路径，卡片不该在用户没操作时消失；挂起不是落定，提问照常等回答。"""
    harness.actor.start()
    harness.queue.put(_user())
    await _settle()

    await _finish(harness.ledger.created[-1].id, status)

    assert harness.expired == expected
    await harness.actor.abort()
