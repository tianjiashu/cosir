"""WorkflowSession 的执行登记：取消信号、释放与「同一 Task 至多一个执行」约束。"""

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.service.conversation_run import workflow_session as session_module
from app.service.conversation_run.workflow_session import WorkflowSession, _Execution

_RUN_ID = 1
_TASK_ID = 7
_RUN = SimpleNamespace(id=_RUN_ID, task_id=_TASK_ID, status="running")


def _execution(thread_task, run_id: int = _RUN_ID) -> _Execution:
    """按 Task 登记的执行替身：只带登记判定需要的 run 身份与后台 task。"""
    execution = _Execution(run=SimpleNamespace(id=run_id, task_id=_TASK_ID), fresh_run=False)
    execution.thread_task = thread_task
    return execution


def _session() -> WorkflowSession:
    """只用执行登记的会话：不装配领域服务。"""
    session = WorkflowSession.__new__(WorkflowSession)
    session._executions = {}
    session._release_signals = {}
    session._closing = False
    return session


def _drive(behavior: Callable[[], Awaitable[None]]):
    async def drive(_run_id: int, *_args: Any) -> None:
        await behavior()

    return drive


@pytest.fixture(autouse=True)
def _isolated_signal_registry():
    """每个用例前后清空进程内取消信号，避免用例间串扰。"""

    cancellation_registry.consume(_TASK_ID)
    yield
    cancellation_registry.consume(_TASK_ID)


@pytest.mark.asyncio
async def test_cancel_marks_signal_and_returns_without_waiting_for_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runner 永不返回时取消依旧立即返回 True，且只标记进程内信号。"""

    async def runner() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(session_module, "drive_run", _drive(runner))
    session = _session()
    session._start(_RUN, fresh_run=True)
    execution = session._executions[_TASK_ID].thread_task
    try:
        assert await asyncio.wait_for(session.cancel(_TASK_ID), 1.0) is True
        assert cancellation_registry.is_cancelled(_TASK_ID) is True
        # 取消只发信号：不取消后台执行 task。
        await asyncio.sleep(0)
        assert not execution.done()
    finally:
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_preserves_task_terminal(monkeypatch, tmp_path) -> None:
    from test_terminal_session_service import make_service

    terminal, workers = make_service(monkeypatch)
    terminal_session = terminal.start(
        task_id=7, workspace_id=1, workspace_root=str(tmp_path), run_id=1
    )
    session = _session()
    session._executions[_TASK_ID] = _execution(asyncio.current_task())
    try:
        assert await session.cancel(_TASK_ID) is True
        assert not workers[0].closed
        terminal.write(
            str(terminal_session["session_id"]),
            task_id=7,
            input={"type": "line", "text": "next run"},
            operation_id="after-run-cancel",
            wait_ms=0,
        )
        assert workers[0].writes == [b"next run\r"]
    finally:
        terminal.shutdown()


@pytest.mark.asyncio
async def test_cancel_is_idempotent_while_execution_is_active() -> None:
    """同一 Task 重复停止都被接受，信号只有一份，释放时一并取走。"""

    session = _session()
    session._executions[_TASK_ID] = _execution(asyncio.current_task())

    assert await session.cancel(_TASK_ID) is True
    assert await session.cancel(_TASK_ID) is True
    assert session._release(_TASK_ID) == "user"
    assert cancellation_registry.is_cancelled(_TASK_ID) is False


@pytest.mark.asyncio
async def test_cancel_missing_task_raises_without_signal() -> None:
    """task 不存在时抛 KeyError（API 映射 404），且不登记信号。"""

    def missing(task_id: int) -> None:
        raise KeyError(task_id)

    session = _session()
    session._tasks = SimpleNamespace(get_task=missing)

    with pytest.raises(KeyError):
        await session.cancel(_TASK_ID)
    assert cancellation_registry.is_cancelled(_TASK_ID) is False


@pytest.mark.asyncio
async def test_start_rejects_second_execution_on_same_task() -> None:
    """同一 Task 至多一个执行：第二次登记直接拒绝，不覆盖已有执行。"""

    session = _session()
    session._executions[_TASK_ID] = _execution(asyncio.current_task())

    with pytest.raises(ValueError):
        session._start(SimpleNamespace(id=2, task_id=_TASK_ID), fresh_run=True)
    assert session._executions[_TASK_ID].run.id == _RUN_ID


@pytest.mark.asyncio
async def test_execution_releases_and_runs_after_run_when_driver_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """驱动逃逸的异常只记录：Run 结束后的调度照常执行，执行登记照常释放。"""

    async def runner() -> None:
        raise RuntimeError("driver died")

    monkeypatch.setattr(session_module, "drive_run", _drive(runner))
    session = _session()
    after: list[int] = []

    async def after_run(run) -> None:
        after.append(run.id)

    monkeypatch.setattr(session, "_after_run", after_run)
    session._start(_RUN, fresh_run=True)
    await session.wait_released(_TASK_ID)

    assert after == [_RUN_ID]
    assert _TASK_ID not in session._executions


@pytest.mark.asyncio
async def test_after_run_failure_still_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    async def runner() -> None:
        return None

    monkeypatch.setattr(session_module, "drive_run", _drive(runner))
    session = _session()

    async def after_run(_run) -> None:
        raise RuntimeError("handoff failed")

    monkeypatch.setattr(session, "_after_run", after_run)
    session._start(_RUN, fresh_run=True)
    cancellation_registry.request_cancel(_TASK_ID)
    await session.wait_released(_TASK_ID)

    assert _TASK_ID not in session._executions
    assert cancellation_registry.is_cancelled(_TASK_ID) is False


@pytest.mark.asyncio
async def test_close_cancels_and_releases_without_after_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关闭期取消：不再调度后续 Run，登记与信号都被清理；遗留 running Run 由重启收敛。"""

    async def runner() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(session_module, "drive_run", _drive(runner))
    session = _session()
    after: list[int] = []

    async def after_run(run) -> None:
        after.append(run.id)

    monkeypatch.setattr(session, "_after_run", after_run)
    session._start(_RUN, fresh_run=True)
    await asyncio.sleep(0.01)
    await session.close()

    assert after == []
    assert _TASK_ID not in session._executions
    assert cancellation_registry.is_cancelled(_TASK_ID) is False
