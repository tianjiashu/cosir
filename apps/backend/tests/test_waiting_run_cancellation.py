"""Task 级取消：有执行时只发信号；挂起的 Run 没有执行者，闸门内直接落定；释放边界是信号唯一
清理点。执行登记、挂起态取消与 Run 结束后的调度都在 ``WorkflowSession``。
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.models.enums.conversation_run_status import ConversationRunStatus, RunTransition
from app.service.conversation_run import workflow_session as session_module
from app.service.conversation_run.workflow_session import WorkflowSession, _Execution

_TASK_ID = 11
_RUN_ID = 7


class _StubServices:
    """run / state / task service 替身：记录状态迁移。"""

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


def _execution(thread_task, run_id: int = _RUN_ID) -> _Execution:
    """按 Task 登记的执行替身：只带登记判定需要的 run 身份与后台 task。"""
    execution = _Execution(run=SimpleNamespace(id=run_id, task_id=_TASK_ID), fresh_run=False)
    execution.thread_task = thread_task
    return execution


def _build(monkeypatch: pytest.MonkeyPatch, services: _StubServices):
    """绕过装配的调度入口；``_start``、推进与取消后续都换成记录器。"""

    session = WorkflowSession.__new__(WorkflowSession)
    session._executions = {}
    session._release_signals = {}
    session._closing = False
    session._runs = services
    session._run_state = services
    session._tasks = services
    monkeypatch.setattr(
        session_module.task_runtime_spaces,
        "get_or_create",
        lambda _: SimpleNamespace(async_operation=lambda **kw: nullcontext()),
    )
    record = SimpleNamespace(started=[], advanced=[], after_cancelled=[])
    monkeypatch.setattr(session, "_start", lambda run, **_kw: record.started.append(run.id))

    async def advance(task_id: int, *, by_user: bool) -> None:
        record.advanced.append((task_id, by_user))

    async def after_cancelled(run: SimpleNamespace, reason: str) -> None:
        record.after_cancelled.append((run.id, reason))

    monkeypatch.setattr(session, "_advance", advance)
    monkeypatch.setattr(session, "_after_cancelled", after_cancelled)
    return session, record


@pytest.fixture(autouse=True)
def _clear_signal() -> None:
    yield
    cancellation_registry.consume(_TASK_ID)


def test_cancel_with_active_execution_only_marks_task_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _StubServices(ConversationRunStatus.RUNNING.value)
    session, record = _build(monkeypatch, services)

    async def scenario() -> bool:
        session._executions[_TASK_ID] = _execution(asyncio.current_task())
        return await session.cancel(_TASK_ID)

    assert asyncio.run(scenario()) is True
    assert cancellation_registry.is_cancelled(_TASK_ID) is True
    assert services.transitions == []
    assert record.after_cancelled == []


def test_cancel_terminal_run_leaves_no_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """已结束的 Run 没有执行者：不登记信号，否则下一次编辑或续跑会一启动就被取消。"""
    services = _StubServices(ConversationRunStatus.COMPLETED.value)
    session, record = _build(monkeypatch, services)

    assert asyncio.run(session.cancel(_TASK_ID)) is False
    assert cancellation_registry.is_cancelled(_TASK_ID) is False
    assert record.started == []


@pytest.mark.parametrize("reason", ["user", "edited"])
def test_cancel_suspended_run_settles_directly_without_starting(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    """挂起的 Run 没有执行者：闸门内直接落定 cancelled，不登记信号、不启动执行。"""
    services = _StubServices(ConversationRunStatus.SUSPENDED.value)
    session, record = _build(monkeypatch, services)

    assert asyncio.run(session.cancel(_TASK_ID, reason=reason)) is True

    assert services.transitions == [(_RUN_ID, RunTransition.CANCEL)]
    assert record.started == []
    assert record.after_cancelled == [(_RUN_ID, reason)]
    assert cancellation_registry.is_cancelled(_TASK_ID) is False


def test_release_consumes_signal_only_for_own_execution() -> None:
    """唤醒与交接会为同一 Task 登记新执行：旧执行的释放不得误删它或取走它的信号。"""
    executor = WorkflowSession.__new__(WorkflowSession)
    executor._executions = {}
    executor._release_signals = {}

    async def scenario() -> tuple[object, object]:
        other = asyncio.get_running_loop().create_future()
        executor._executions[_TASK_ID] = _execution(other)
        cancellation_registry.request_cancel(_TASK_ID)
        foreign = executor._release(_TASK_ID)
        executor._executions[_TASK_ID] = _execution(asyncio.current_task())
        own = executor._release(_TASK_ID)
        other.cancel()
        return foreign, own

    assert asyncio.run(scenario()) == (None, "user")
    assert cancellation_registry.is_cancelled(_TASK_ID) is False


def test_release_after_late_cancel_on_completed_run_skips_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最后检查点之后才到的停止：Run 已完成，释放时不为队列消息接续新 Run。"""
    services = _StubServices(ConversationRunStatus.COMPLETED.value)
    session, record = _build(monkeypatch, services)

    async def scenario() -> None:
        session._executions[_TASK_ID] = _execution(asyncio.current_task())
        cancellation_registry.request_cancel(_TASK_ID)
        await session._after_run(services.get_run(_RUN_ID))

    asyncio.run(scenario())
    assert record.advanced == []
    assert _TASK_ID not in session._executions
    assert cancellation_registry.is_cancelled(_TASK_ID) is False


def test_suspend_release_rechecks_queue_so_no_wake_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """挂起落定后在闸门内释放并推进一次：挂起前后到达的可唤醒消息不会丢。"""
    services = _StubServices(ConversationRunStatus.SUSPENDED.value)
    session, record = _build(monkeypatch, services)

    async def scenario() -> None:
        session._executions[_TASK_ID] = _execution(asyncio.current_task())
        await session._after_run(services.get_run(_RUN_ID))

    asyncio.run(scenario())
    assert record.advanced == [(_TASK_ID, False)]
    assert services.transitions == []


def test_cancel_arriving_while_suspending_settles_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消落在 running → suspended 窗口：释放时取走信号，直接落定 cancelled，不再推进。"""
    services = _StubServices(ConversationRunStatus.SUSPENDED.value)
    session, record = _build(monkeypatch, services)

    async def scenario() -> None:
        session._executions[_TASK_ID] = _execution(asyncio.current_task())
        cancellation_registry.request_cancel(_TASK_ID)
        await session._after_run(services.get_run(_RUN_ID))

    asyncio.run(scenario())
    assert services.transitions == [(_RUN_ID, RunTransition.CANCEL)]
    assert record.after_cancelled == [(_RUN_ID, "user")]
    assert record.advanced == []
    assert cancellation_registry.is_cancelled(_TASK_ID) is False
