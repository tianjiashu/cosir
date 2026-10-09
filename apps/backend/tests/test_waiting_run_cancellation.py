"""「等待用户决定」的 Run 被取消时的收口契约。

背景：取消信号是**进程内**的（``cancellation_registry``），只在节点执行时被检查（``model_node``
请求前 / 流式循环内、``structured_output_node``、各工具 handler）。而等待态下图是**挂起**的——没有
任何节点在跑，信号无人消费：用户点了停止却毫无反应，还要等他先作答才生效（表现为「点停止 → 又点
确认 → 整轮才取消」）。

因此取消等待中的 Run 时必须**重新驱动**：先条件迁回 ``running``，再以 ``resume`` 模式从 model 节点
重跑，让 model 节点检出信号并落 cancelled。本模块钉住这条路径与它的两个边界：

- 只有等待态才重新驱动（running 的 Run 已有驱动者在跑，再开一个会变成双驱动）；
- 重新驱动会丢掉尚未消费的 human-in-the-loop 断点，因此 ``workflow`` 的续跑守卫只对「未标记取消」
  的 Run 拦截（``_reject_resume_without_decisions``）——取消路径是这条守卫的唯一例外。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.runtime.conversation_run_executor import ConversationRunExecutor
from app.core.workflows.react.workflow import _reject_resume_without_decisions
from app.models.enums.conversation_run_status import ConversationRunStatus

_RUN_ID = 7


class _StubRunService:
    """run service / state service 替身：记录续跑迁移调用并把状态切回 running。"""

    def __init__(self, status: str) -> None:
        self.status = status
        self.resume_calls: list[int] = []

    def get_run(self, run_id: int) -> SimpleNamespace:
        return SimpleNamespace(id=run_id, task_id=1, status=self.status)

    def resume_waiting_run(self, run_id: int) -> SimpleNamespace | None:
        self.resume_calls.append(run_id)
        self.status = ConversationRunStatus.RUNNING.value
        return SimpleNamespace(id=run_id, task_id=1, status=self.status)


class _StubTerminal:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def close_run_terminals(self, _run_id: int, *, reason: str) -> None:
        self.calls.append(reason)


def _build_executor(
    monkeypatch: pytest.MonkeyPatch, service: _StubRunService
) -> tuple[ConversationRunExecutor, list[tuple[int, str]]]:
    """绕过依赖装配构造执行器，并把 ``start`` 换成记录器（不真的建后台 task）。"""

    executor = ConversationRunExecutor.__new__(ConversationRunExecutor)
    executor._run_service = service
    executor._run_state_service = service
    executor._terminal_session_service = _StubTerminal()
    executor._event_projector = None
    executor._executions = {}
    executor._signal = cancellation_registry
    started: list[tuple[int, str]] = []

    async def _start(run_id: int, start_mode: str, user_decisions: Any = ()) -> None:
        started.append((run_id, start_mode))

    monkeypatch.setattr(executor, "start", _start)
    return executor, started


@pytest.fixture(autouse=True)
def _clear_signal() -> None:
    """每个用例结束后清掉进程内取消信号，避免污染其它用例。"""

    yield
    cancellation_registry.clear(_RUN_ID)


def test_cancel_waiting_run_resumes_so_the_signal_has_a_node_to_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """等待态取消：迁回 running 并以续跑模式重新驱动，取消信号才有节点去读。"""

    service = _StubRunService(ConversationRunStatus.WAITING_FOR_INPUT.value)
    executor, started = _build_executor(monkeypatch, service)

    assert asyncio.run(executor.cancel(_RUN_ID)) is True

    assert service.resume_calls == [_RUN_ID]
    # 不带决定：workflow 走 Command(goto=model)，由 model 节点检出信号落 cancelled。
    assert started == [(_RUN_ID, "resume")]
    assert cancellation_registry.is_cancelled(_RUN_ID) is True


def test_cancel_running_run_does_not_spawn_a_second_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """running 的 Run 已有驱动者在跑：只标记信号，绝不新增第二个驱动。"""

    service = _StubRunService(ConversationRunStatus.RUNNING.value)
    executor, started = _build_executor(monkeypatch, service)

    assert asyncio.run(executor.cancel(_RUN_ID)) is True

    assert started == []
    assert service.resume_calls == []
    assert cancellation_registry.is_cancelled(_RUN_ID) is True


def test_reject_resume_without_decisions_allows_cancellation_path() -> None:
    """续跑守卫：默认拒绝绕开断点；只有「已标记取消」的 Run 才放行。"""

    assert _reject_resume_without_decisions(False, _RUN_ID) is False
    assert _reject_resume_without_decisions(True, _RUN_ID) is True
    cancellation_registry.mark_cancelled(_RUN_ID)
    # 取消路径：丢弃未消费的断点是预期行为（终态由 model_node 落定）。
    assert _reject_resume_without_decisions(True, _RUN_ID) is False
