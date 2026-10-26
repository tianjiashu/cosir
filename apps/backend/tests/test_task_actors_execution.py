"""调度入口与 TaskActor 的执行登记：取消信号、RunFinished 收尾与「同一 Task 至多一个执行」约束。"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.service import depends as service_depends
from app.core import task_actor as actor_module
from app.core.task_actor import Execution, TaskActor
from app.core.task_actors import TaskActors

_RUN_ID = 1
_TASK_ID = 7
_RUN = SimpleNamespace(id=_RUN_ID, task_id=_TASK_ID, status="running")


class _Services:
    """task / run / queue 替身：Task 存在且最新 Run 为 ``_RUN``；``missing`` 时 Task 不存在。"""

    def __init__(self, *, missing: bool = False) -> None:
        self.missing = missing

    def get_task(self, task_id: int) -> SimpleNamespace:
        if self.missing:
            raise KeyError(task_id)
        return SimpleNamespace(id=task_id, task_type="user", parent_task_id=None)

    def get_latest_run(self, _task_id: int) -> SimpleNamespace:
        return _RUN

    def require_latest_run(self, task_id: int) -> SimpleNamespace:
        self.get_task(task_id)
        return _RUN

    def get_run(self, _run_id: int) -> SimpleNamespace:
        return _RUN

    def pending(self, _task_id: int) -> list:
        return []


def _session(monkeypatch: pytest.MonkeyPatch, services: _Services | None = None) -> TaskActors:
    """接替身服务的调度入口；actor 闸门换成空上下文。"""
    services = services or _Services()
    for getter in (
        "get_conversation_run_ledger",
        "get_task_service",
        "get_task_message_queue",
        "get_user_input_service",
    ):
        monkeypatch.setattr(service_depends, getter, lambda: services)
    monkeypatch.setattr(
        actor_module.task_runtime_spaces,
        "get_or_create",
        lambda _: SimpleNamespace(async_operation=lambda **kw: nullcontext()),
    )
    return TaskActors()


def _drive(behavior: Callable[[], Awaitable[None]]):
    async def drive(_run_id: int, *_args: Any) -> None:
        await behavior()

    return drive


def _start(session: TaskActors) -> TaskActor:
    """在 actor 上启动 ``_RUN`` 的执行，并放行初始投递（生产由启动它的那批信号处理完放行）。"""
    actor = session._actor(_TASK_ID)
    actor._start(_RUN, fresh_run=True)
    actor.execution.ready.set()
    return actor


async def _until_released(actor: TaskActor) -> None:
    """等执行收尾注销：执行结束后发 RunFinished，actor 处理完那一批才注销登记。"""
    while actor.execution is not None:
        await asyncio.wait_for(actor.state_changed.wait(), 1)


def _fake_execution(actor: TaskActor) -> Execution:
    execution = Execution(run=_RUN, fresh_run=False)
    execution.thread_task = asyncio.current_task()
    actor.execution = execution
    return execution


@pytest.fixture(autouse=True)
def _isolated_signal_registry():
    """每个用例前后清空进程内取消信号，避免用例间串扰。"""

    cancellation_registry.consume(_TASK_ID)
    yield
    cancellation_registry.consume(_TASK_ID)


@pytest.mark.asyncio
async def test_cancel_marks_signal_and_returns_without_waiting_for_runner(monkeypatch) -> None:
    """runner 永不返回时取消依旧立即返回，且只标记进程内信号。"""

    async def runner() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(actor_module, "drive_run", _drive(runner))
    session = _session(monkeypatch)
    execution = _start(session).execution.thread_task
    try:
        session.cancel(_TASK_ID)
        assert cancellation_registry.is_cancelled(_TASK_ID) is True
        # 取消只发信号：不取消后台执行 task。
        await asyncio.sleep(0)
        assert not execution.done()
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_cancel_preserves_task_terminal(monkeypatch, tmp_path) -> None:
    from test_terminal_session_service import make_service

    terminal, workers = make_service(monkeypatch)
    terminal_session = terminal.start(
        task_id=7, workspace_id=1, workspace_root=str(tmp_path), run_id=1
    )
    session = _session(monkeypatch)
    _fake_execution(session._actor(_TASK_ID))
    try:
        session.cancel(_TASK_ID)
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
        session._actors[_TASK_ID].loop_task.cancel()


@pytest.mark.asyncio
async def test_cancel_is_idempotent_while_execution_is_active(monkeypatch) -> None:
    """同一 Task 重复停止都被接受，信号只有一份，收尾时一并取走。"""

    session = _session(monkeypatch)
    actor = session._actor(_TASK_ID)
    _fake_execution(actor)

    session.cancel(_TASK_ID)
    session.cancel(_TASK_ID)
    assert cancellation_registry.consume(_TASK_ID) == "user"
    assert cancellation_registry.is_cancelled(_TASK_ID) is False
    actor.loop_task.cancel()


@pytest.mark.asyncio
async def test_cancel_missing_task_leaves_no_signal(monkeypatch) -> None:
    """task 不存在时投递不报错（存在性由 API 入口校验映射 404），actor 只记录，不登记信号。"""

    session = _session(monkeypatch, _Services(missing=True))
    actor = session._actor(_TASK_ID)
    changed = actor.state_changed

    session.cancel(_TASK_ID)
    await asyncio.wait_for(changed.wait(), 1)
    assert cancellation_registry.is_cancelled(_TASK_ID) is False
    await session.close()


@pytest.mark.asyncio
async def test_start_rejects_second_execution_on_same_task(monkeypatch) -> None:
    """同一 Task 至多一个执行：第二次登记直接拒绝，不覆盖已有执行。"""

    session = _session(monkeypatch)
    actor = session._actor(_TASK_ID)
    _fake_execution(actor)

    with pytest.raises(ValueError):
        actor._start(SimpleNamespace(id=2, task_id=_TASK_ID), fresh_run=True)
    assert actor.execution.run.id == _RUN_ID
    actor.loop_task.cancel()


@pytest.mark.asyncio
async def test_execution_finishes_and_runs_after_run_when_driver_raises(monkeypatch) -> None:
    """驱动逃逸的异常只记录：执行照常发 RunFinished，收尾照常执行并注销。"""

    async def runner() -> None:
        raise RuntimeError("driver died")

    monkeypatch.setattr(actor_module, "drive_run", _drive(runner))
    session = _session(monkeypatch)
    actor = session._actor(_TASK_ID)
    after: list[int] = []

    async def after_run(run, _reason) -> None:
        after.append(run.id)

    monkeypatch.setattr(actor, "_after_run", after_run)
    actor._start(_RUN, fresh_run=True)
    actor.execution.ready.set()
    await _until_released(actor)

    assert after == [_RUN_ID]
    assert actor.execution is None
    await session.close()


@pytest.mark.asyncio
async def test_after_run_failure_still_releases(monkeypatch) -> None:
    async def runner() -> None:
        return None

    monkeypatch.setattr(actor_module, "drive_run", _drive(runner))
    session = _session(monkeypatch)
    actor = session._actor(_TASK_ID)

    async def after_run(_run, _reason) -> None:
        raise RuntimeError("handoff failed")

    monkeypatch.setattr(actor, "_after_run", after_run)
    actor._start(_RUN, fresh_run=True)
    actor.execution.ready.set()
    cancellation_registry.request_cancel(_TASK_ID)
    await _until_released(actor)

    assert actor.execution is None
    assert cancellation_registry.is_cancelled(_TASK_ID) is False
    await session.close()


@pytest.mark.asyncio
async def test_close_cancels_and_releases_without_after_run(monkeypatch) -> None:
    """关闭期取消：不再调度后续 Run，登记与信号都被清理；遗留 running Run 由重启收敛。"""

    async def runner() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(actor_module, "drive_run", _drive(runner))
    session = _session(monkeypatch)
    actor = _start(session)
    after: list[int] = []

    async def after_run(run, _reason) -> None:
        after.append(run.id)

    monkeypatch.setattr(actor, "_after_run", after_run)
    await asyncio.sleep(0.01)
    await session.close()

    assert after == []
    assert session._actors == {}
    assert cancellation_registry.is_cancelled(_TASK_ID) is False
