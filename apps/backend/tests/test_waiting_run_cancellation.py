"""「等待用户决定」的 Run 被取消时的收口契约。

背景：取消信号是**进程内**的（``cancellation_registry``），只在节点执行时被检查（``model_node``
请求前 / 流式循环内、``structured_output_node``、各工具 handler）。而等待态下图是**挂起**的——没有
任何节点在跑，信号无人消费：用户点了停止却毫无反应，还要等他先作答才生效（表现为「点停止 → 又点
确认 → 整轮才取消」）。

因此取消等待中的 Run 时必须**重新驱动**：先条件迁回 ``running``，再以 ``resume_with_input`` 模式
驱动，并注入取消入口自己构造的「强行拒绝」决定（``_force_reject_decision``）。``wait_user`` 把它
归一为对当前唯一待决请求的驳回：断点被真正消费，图推进到 ``model``，由它检出信号并落 cancelled，
断点留在模型侧。本模块钉住这条路径与它的边界：

- 只有等待态才重新驱动（running 的 Run 已有驱动者在跑，再开一个会变成双驱动）；
- 决定由**取消入口**构造而不是 workflow：它不核对 ``request_id``（``FORCE_REJECT``），因此取消
  链路无需读 checkpoint 去定位待决请求，图状态知识不必搬进执行器；
- 不能退化成 ``resume`` + ``goto model`` 绕过断点：LangGraph 会重放 ``wait_user`` 并再次挂起，断点
  落回原处，随后任何不带决定的续跑都会被 workflow 守卫拒绝（「停止后继续」总是失败）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.runtime.conversation_run_executor import (
    ConversationRunExecutor,
)
from app.core.tools.schemas import UserDecision, UserDecisionKind
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
) -> tuple[ConversationRunExecutor, list[tuple[int, str, object]]]:
    """绕过依赖装配构造执行器，并把 ``start`` 换成记录器（不真的建后台 task）。"""

    executor = ConversationRunExecutor.__new__(ConversationRunExecutor)
    executor._run_service = service
    executor._run_state_service = service
    executor._terminal_session_service = _StubTerminal()
    executor._event_projector = None
    executor._executions = {}
    executor._signal = cancellation_registry
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


def test_cancel_waiting_run_resumes_so_the_signal_has_a_node_to_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """等待态取消：迁回 running 并带强行拒绝决定重新驱动，断点被消费、取消信号才有节点读。"""

    service = _StubRunService(ConversationRunStatus.WAITING_FOR_INPUT.value)
    executor, started = _build_executor(monkeypatch, service)

    assert asyncio.run(executor.cancel(_RUN_ID)) is True

    assert service.resume_calls == [_RUN_ID]
    assert len(started) == 1
    run_id, start_mode, decision = started[0]
    assert (run_id, start_mode) == (_RUN_ID, "resume_with_input")
    # 决定由取消入口构造：不核对 request_id，因此断点能被 wait_user 归一为驳回并消费掉。
    assert isinstance(decision, UserDecision)
    assert decision.kind is UserDecisionKind.FORCE_REJECT
    assert decision.request_id == ""
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
