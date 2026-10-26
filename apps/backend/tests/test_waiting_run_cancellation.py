"""Task 级取消：有执行时只发信号；挂起的 Run 没有执行者，actor 直接落定；``RunFinished`` 收尾是
信号唯一清理点。执行登记、挂起态取消与 Run 结束后的调度都在 ``TaskActor``。
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.models.enums.conversation_run_status import ConversationRunStatus, RunTransition
from app.service import depends as service_depends
from app.core import task_actor as actor_module
from app.core.task_actor import Execution, TaskActor
from app.core.task_signals import Cancel, RunFinished

_TASK_ID = 11
_RUN_ID = 7


class _StubServices:
    """run / state / task / queue service 替身：记录状态迁移。"""

    def __init__(self, status: str) -> None:
        self.status = status
        self.transitions: list[tuple[int, RunTransition]] = []

    def get_run(self, run_id: int) -> SimpleNamespace:
        return SimpleNamespace(id=run_id, task_id=_TASK_ID, status=self.status)

    def transition(
        self, run_id: int, transition: RunTransition, **_kwargs: object
    ) -> object | None:
        if self.status not in {status.value for status in transition.sources}:
            return None
        self.transitions.append((run_id, transition))
        self.status = transition.target.value
        return self.get_run(run_id)

    def get_task(self, task_id: int) -> SimpleNamespace:
        # 用户 Task：Run 结束后不向父 Task 回投结论。
        return SimpleNamespace(id=task_id, task_type="user", parent_task_id=None)

    def get_latest_run(self, _task_id: int) -> SimpleNamespace:
        return self.get_run(_RUN_ID)

    def require_latest_run(self, task_id: int) -> SimpleNamespace:
        return self.get_latest_run(task_id)

    def pending(self, _task_id: int) -> list:
        return []


def _build(monkeypatch: pytest.MonkeyPatch, services: _StubServices):
    """在当前事件循环上拉起一个接替身服务的 actor；推进与取消后续换成记录器。"""

    for getter in (
        "get_conversation_run_ledger",
        "get_task_service",
        "get_task_message_queue",
    ):
        monkeypatch.setattr(service_depends, getter, lambda: services)
    monkeypatch.setattr(
        actor_module.task_runtime_spaces,
        "get_or_create",
        lambda _: SimpleNamespace(async_operation=lambda **kw: nullcontext()),
    )
    session = SimpleNamespace(evict=lambda _actor: None)
    actor = TaskActor(_TASK_ID, session)
    record = SimpleNamespace(started=[], advanced=[], after_cancelled=[])
    monkeypatch.setattr(actor, "_start", lambda run, **_kw: record.started.append(run.id))

    async def advance(*, by_user: bool, project: bool = False) -> None:
        record.advanced.append(by_user)

    async def after_cancelled(run: SimpleNamespace, reason: str) -> None:
        record.after_cancelled.append((run.id, reason))

    monkeypatch.setattr(actor, "_advance", advance)
    monkeypatch.setattr(actor, "_after_cancelled", after_cancelled)
    return actor, record


def _register_execution(actor: TaskActor) -> Execution:
    """按 Task 登记的执行替身：只带收尾需要的 run 身份。"""
    execution = Execution(run=SimpleNamespace(id=_RUN_ID, task_id=_TASK_ID), fresh_run=False)
    actor.execution = execution
    return execution


async def _processed(actor: TaskActor, signal: Cancel | RunFinished) -> None:
    """投递一个信号并等 actor 处理完这一批：信号没有回执，以批处理后的状态通知为准。"""
    changed = actor.state_changed
    actor.send(signal)
    await asyncio.wait_for(changed.wait(), 1)


async def _cancel(actor: TaskActor, reason: str = "user") -> None:
    await _processed(actor, Cancel(reason))


async def _finish(actor: TaskActor, execution: Execution) -> None:
    await _processed(actor, RunFinished(execution))


@pytest.fixture(autouse=True)
def _clear_signal() -> None:
    yield
    cancellation_registry.consume(_TASK_ID)


@pytest.mark.asyncio
async def test_cancel_with_active_execution_only_marks_task_signal(monkeypatch) -> None:
    services = _StubServices(ConversationRunStatus.RUNNING.value)
    actor, record = _build(monkeypatch, services)
    _register_execution(actor)

    await _cancel(actor)
    assert cancellation_registry.is_cancelled(_TASK_ID) is True
    assert services.transitions == []
    assert record.after_cancelled == []
    actor.loop_task.cancel()


@pytest.mark.asyncio
async def test_cancel_terminal_run_leaves_no_signal(monkeypatch) -> None:
    """已结束的 Run 没有执行者：不登记信号，否则下一次编辑或续跑会一启动就被取消。"""
    services = _StubServices(ConversationRunStatus.COMPLETED.value)
    actor, record = _build(monkeypatch, services)

    await _cancel(actor)
    assert cancellation_registry.is_cancelled(_TASK_ID) is False
    assert record.started == []
    actor.loop_task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["user", "edited"])
async def test_cancel_suspended_run_settles_directly_without_starting(
    monkeypatch, reason: str
) -> None:
    """挂起的 Run 没有执行者：actor 直接落定 cancelled，不登记信号、不启动执行。"""
    services = _StubServices(ConversationRunStatus.SUSPENDED.value)
    actor, record = _build(monkeypatch, services)

    await _cancel(actor, reason)

    assert services.transitions == [(_RUN_ID, RunTransition.CANCEL)]
    assert record.started == []
    assert record.after_cancelled == [(_RUN_ID, reason)]
    assert cancellation_registry.is_cancelled(_TASK_ID) is False
    actor.loop_task.cancel()


@pytest.mark.asyncio
async def test_late_cancel_on_completed_run_skips_handoff(monkeypatch) -> None:
    """最后检查点之后才到的停止：Run 已完成，收尾时不为队列消息接续新 Run。"""
    services = _StubServices(ConversationRunStatus.COMPLETED.value)
    actor, record = _build(monkeypatch, services)
    execution = _register_execution(actor)
    cancellation_registry.request_cancel(_TASK_ID)

    await _finish(actor, execution)

    assert record.advanced == []
    assert actor.execution is None
    assert cancellation_registry.is_cancelled(_TASK_ID) is False
    actor.loop_task.cancel()


@pytest.mark.asyncio
async def test_suspend_settle_rechecks_queue_so_no_wake_is_lost(monkeypatch) -> None:
    """挂起落定后收尾时推进一次：挂起前后到达的可唤醒消息不会丢。"""
    services = _StubServices(ConversationRunStatus.SUSPENDED.value)
    actor, record = _build(monkeypatch, services)
    execution = _register_execution(actor)

    await _finish(actor, execution)

    assert record.advanced == [False]
    assert services.transitions == []
    actor.loop_task.cancel()


@pytest.mark.asyncio
async def test_cancel_arriving_while_suspending_settles_cancelled(monkeypatch) -> None:
    """取消落在 running → suspended 窗口：收尾时取走信号，直接落定 cancelled，不再推进。"""
    services = _StubServices(ConversationRunStatus.SUSPENDED.value)
    actor, record = _build(monkeypatch, services)
    execution = _register_execution(actor)
    cancellation_registry.request_cancel(_TASK_ID)

    await _finish(actor, execution)

    assert services.transitions == [(_RUN_ID, RunTransition.CANCEL)]
    assert record.after_cancelled == [(_RUN_ID, "user")]
    assert record.advanced == []
    assert cancellation_registry.is_cancelled(_TASK_ID) is False
    actor.loop_task.cancel()
