"""Task 级取消：只发信号；等待态以强制拒绝恢复后交循环收敛；释放边界是信号唯一清理点。

执行登记、等待态取消与 Run 结束后的调度都在 ``WorkflowSession``。
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.models.enums.conversation_run_status import ConversationRunStatus, RunTransition
from app.models.user_decision import UserDecisionKind
from app.service.conversation_run import workflow_session as session_module
from app.service.conversation_run.workflow_session import WorkflowSession, _Execution

_TASK_ID = 11
_RUN_ID = 7


class _StubServices:
    """run / state / task service 替身：记录等待恢复调用。"""

    def __init__(self, status: str, *, resumable: bool = True) -> None:
        self.status = status
        self.resumable = resumable
        self.resume_calls: list[tuple[int, object]] = []

    def get_run(self, run_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            id=run_id,
            task_id=_TASK_ID,
            status=self.status,
            execution_data=SimpleNamespace(
                continuation=SimpleNamespace(observations=[{"tool_call_id": "call-1"}])
            ),
        )

    def transition(
        self, run_id: int, transition: RunTransition, *, user_decision: object
    ) -> object | None:
        assert transition is RunTransition.RESUME_WITH_DECISION
        self.resume_calls.append((run_id, user_decision))
        if not self.resumable:
            return None
        self.status = ConversationRunStatus.RUNNING.value
        return self.get_run(run_id)

    def get_task(self, task_id: int) -> SimpleNamespace:
        return SimpleNamespace(id=task_id)

    def get_latest_run(self, _task_id: int) -> SimpleNamespace:
        return self.get_run(_RUN_ID)

    def list_child_tasks(self, _task_id: int) -> list[SimpleNamespace]:
        return []


def _build(
    monkeypatch: pytest.MonkeyPatch, services: _StubServices
) -> tuple[WorkflowSession, WorkflowSession, list[int]]:
    """绕过装配的调度入口；``_start`` 换成记录器，不真的建后台 task。"""

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
    started: list[int] = []
    monkeypatch.setattr(
        session, "_start", lambda run, **_kw: started.append(run.id)
    )
    return session, session, started


@pytest.fixture(autouse=True)
def _clear_signal() -> None:
    yield
    cancellation_registry.consume(_TASK_ID)


def _register_execution(executor: WorkflowSession, task: asyncio.Task) -> None:
    executor._executions[_RUN_ID] = _Execution(task_id=_TASK_ID, thread_task=task)


def test_cancel_with_active_execution_only_marks_task_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _StubServices(ConversationRunStatus.RUNNING.value)
    session, executor, started = _build(monkeypatch, services)

    async def scenario() -> bool:
        _register_execution(executor, asyncio.current_task())
        return await session.cancel(_TASK_ID)

    assert asyncio.run(scenario()) is True
    assert cancellation_registry.is_cancelled(_TASK_ID) is True
    assert started == []
    assert services.resume_calls == []


def test_cancel_terminal_run_leaves_no_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """已结束的 Run 没有执行者：不登记信号，否则下一次编辑或续跑会一启动就被取消。"""
    services = _StubServices(ConversationRunStatus.COMPLETED.value)
    session, _, started = _build(monkeypatch, services)

    assert asyncio.run(session.cancel(_TASK_ID)) is False
    assert cancellation_registry.is_cancelled(_TASK_ID) is False
    assert started == []


def test_cancel_waiting_run_resumes_with_force_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    services = _StubServices(ConversationRunStatus.WAITING_FOR_INPUT.value)
    session, _, started = _build(monkeypatch, services)

    assert asyncio.run(session.cancel(_TASK_ID)) is True

    [(run_id, decision)] = services.resume_calls
    assert run_id == _RUN_ID
    assert decision.kind is UserDecisionKind.FORCE_REJECT
    assert started == [_RUN_ID]
    # 信号留给恢复后的循环在 MODEL 检查点消费。
    assert cancellation_registry.is_cancelled(_TASK_ID) is True


def test_cancel_waiting_run_that_lost_resume_race_drops_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """提交决定先赢得 waiting → running 迁移：取消不登记残留信号，返回不可取消。"""
    services = _StubServices(ConversationRunStatus.WAITING_FOR_INPUT.value, resumable=False)
    session, _, started = _build(monkeypatch, services)

    assert asyncio.run(session.cancel(_TASK_ID)) is False
    assert started == []
    assert cancellation_registry.is_cancelled(_TASK_ID) is False


def test_release_consumes_signal_only_for_own_execution() -> None:
    """等待态取消以同一 run_id 登记新执行：旧执行的释放不得误删它或取走它的信号。"""
    executor = WorkflowSession.__new__(WorkflowSession)
    executor._executions = {}
    executor._release_signals = {}

    async def scenario() -> tuple[bool, bool]:
        other = asyncio.get_running_loop().create_future()
        executor._executions[_RUN_ID] = _Execution(task_id=_TASK_ID, thread_task=other)
        cancellation_registry.request_cancel(_TASK_ID)
        foreign = executor._release(_RUN_ID)
        _register_execution(executor, asyncio.current_task())
        own = executor._release(_RUN_ID)
        other.cancel()
        return foreign, own

    assert asyncio.run(scenario()) == (False, True)
    assert cancellation_registry.is_cancelled(_TASK_ID) is False


def test_release_after_late_cancel_on_completed_run_skips_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最后检查点之后才到的停止：Run 已完成，释放时不为 inbox 接续新 Run。"""
    services = _StubServices(ConversationRunStatus.COMPLETED.value)
    session, executor, started = _build(monkeypatch, services)
    handoffs: list[int] = []

    async def record_handoff(task_id: int) -> None:
        handoffs.append(task_id)

    monkeypatch.setattr(session, "_advance", record_handoff)

    async def scenario() -> None:
        _register_execution(executor, asyncio.current_task())
        cancellation_registry.request_cancel(_TASK_ID)
        await session._after_run(services.get_run(_RUN_ID))

    asyncio.run(scenario())
    assert handoffs == []
    assert started == []
    assert _RUN_ID not in executor._executions
    assert cancellation_registry.is_cancelled(_TASK_ID) is False


def test_release_after_cancel_during_suspend_resumes_for_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消落在 running → waiting 窗口：释放时接手，以强制拒绝恢复，而不是留在等待态。"""
    services = _StubServices(ConversationRunStatus.WAITING_FOR_INPUT.value)
    session, executor, started = _build(monkeypatch, services)

    async def scenario() -> None:
        _register_execution(executor, asyncio.current_task())
        cancellation_registry.request_cancel(_TASK_ID)
        await session._after_run(services.get_run(_RUN_ID))

    asyncio.run(scenario())
    assert started == [_RUN_ID]
    assert services.resume_calls[0][1].kind is UserDecisionKind.FORCE_REJECT
    assert cancellation_registry.is_cancelled(_TASK_ID) is True
