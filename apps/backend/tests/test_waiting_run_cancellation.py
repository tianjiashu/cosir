"""等待态取消直接收敛持久化请求；运行态取消仅发送协作信号。"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.runtime.conversation_run_executor import (
    ConversationRunExecutor,
)
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

_RUN_ID = 7


class _StubRunService:
    """run service / state service 替身：记录等待取消调用。"""

    def __init__(self, status: str) -> None:
        self.status = status
        self.cancel_calls: list[int] = []

    def get_run(self, run_id: int) -> SimpleNamespace:
        return SimpleNamespace(id=run_id, task_id=1, status=self.status)

    def cancel_waiting_run(self, run_id: int) -> SimpleNamespace | None:
        self.cancel_calls.append(run_id)
        self.status = ConversationRunStatus.CANCELLED.value
        return SimpleNamespace(id=run_id, task_id=1, status=self.status)


class _StubTerminal:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def close_run_terminals(self, _run_id: int, *, reason: str) -> None:
        self.calls.append(reason)


def _build_executor(
    monkeypatch: pytest.MonkeyPatch, service: _StubRunService
) -> tuple[ConversationRunExecutor, list[tuple[int, str, object]]]:
    """绕过依赖装配构造执行器，并把 ``start`` 换成记录器（不真的建后台 task）。"""

    executor = ConversationRunExecutor.__new__(ConversationRunExecutor)
    executor._run_service = service
    executor._run_state_service = service
    executor._terminal_session_service = _StubTerminal()
    executor._event_projector = None
    executor._executions = {}
    executor._signal = cancellation_registry
    monkeypatch.setattr(
        task_runtime_spaces,
        "get_or_create",
        lambda _: SimpleNamespace(async_operation=lambda **kw: nullcontext()),
    )
    started: list[tuple[int, str, object]] = []

    async def _start(run_id: int, start_mode: str, user_decision: object = None) -> None:
        started.append((run_id, start_mode, user_decision))

    monkeypatch.setattr(executor, "start", _start)
    return executor, started


@pytest.fixture(autouse=True)
def _clear_signal() -> None:
    """每个用例结束后清掉进程内取消信号，避免污染其它用例。"""

    yield
    cancellation_registry.clear(_RUN_ID)


def test_cancel_waiting_run_settles_without_starting_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """等待态直接关闭待决请求，不创建模型执行者。"""
    service = _StubRunService(ConversationRunStatus.WAITING_FOR_INPUT.value)
    executor, started = _build_executor(monkeypatch, service)
    assert asyncio.run(executor.cancel(_RUN_ID)) is True
    assert service.cancel_calls == [_RUN_ID]
    assert service.status == ConversationRunStatus.CANCELLED.value
    assert started == []
    assert cancellation_registry.is_cancelled(_RUN_ID) is False


def test_cancel_running_run_does_not_spawn_a_second_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """running 的 Run 已有驱动者在跑：只标记信号，绝不新增第二个驱动。"""

    service = _StubRunService(ConversationRunStatus.RUNNING.value)
    executor, started = _build_executor(monkeypatch, service)

    assert asyncio.run(executor.cancel(_RUN_ID)) is True

    assert started == []
    assert service.cancel_calls == []
    assert cancellation_registry.is_cancelled(_RUN_ID) is True
