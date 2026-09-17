"""ChildAgentRunner 认领 child run 的回归测试。

背景（2026-09-17 缺陷）：委派链路的 child run 由 ``conversation_run_service.create_run``
建成 ``pending`` 后从未被认领（``claim_pending_run`` 当时只有主链路两处调用点），而
``ConversationRunExecutor.start`` 断言 run 必须是 ``running`` ⇒ 每次委派都在 13ms 内以
``run N is not running`` 失败（``delegations.error``、日志事件 ``delegation_child_run_failed``）。

本文件锁死两条不变量：
1. ``ChildAgentRunner`` 必须在调用执行器 ``start`` **之前**认领 child run；
2. child run 不可认领（已终态/已被他人认领）时必须收敛为 failed，且不得启动执行。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.core.delegation import child_agent_runner as runner_module
from app.core.delegation.child_agent_runner import ChildAgentRunner


class _FakeRunStateService:
    """以 ``status`` 模拟 child run 的真实持久化状态。"""

    def __init__(self, status: str = "pending") -> None:
        self.status = status
        self.claims: list[int] = []

    def claim_pending_run(self, run_id: int) -> object | None:
        """仅当仍为 pending 时认领成功，并推进为 running（与生产白名单一致）。"""

        self.claims.append(run_id)
        if self.status != "pending":
            return None
        self.status = "running"
        return SimpleNamespace(id=run_id, status="running")

    def get_run(self, run_id: int) -> object:
        """返回当前状态的 run 快照。"""

        return SimpleNamespace(id=run_id, status=self.status, final_output="child done")

    def fail_run_if_running(
        self, run_id: int, *, end_reason: str = "", final_output: str | None = None
    ) -> None:
        """失败收敛替身：真实实现会对非 running 的 run 返回 None。"""

        return None


class _FakeExecutor:
    """复刻 ``ConversationRunExecutor.start`` 的「run 必须 running」前置断言。"""

    def __init__(self, run_state: _FakeRunStateService) -> None:
        self._run_state = run_state
        self.started: list[int] = []

    async def start(self, run_id: int, runner: object) -> object:
        """若 run 不是 running 则抛 ValueError（与生产文案一致）。"""

        if self._run_state.status != "running":
            raise ValueError(f"run {run_id} is not running")
        self.started.append(run_id)
        self._run_state.status = "completed"

        async def _completed() -> None:
            return None

        return _completed()


async def _noop_agent(*_args: object, **_kwargs: object) -> None:
    """最小 run_agent 替身。"""

    return None


def _child_profile(run_id: int) -> object:
    """构造只带 run 标识的 child profile 替身。"""

    return SimpleNamespace(run=SimpleNamespace(id=run_id))


def _wire(monkeypatch, run_state: _FakeRunStateService) -> _FakeExecutor:
    """把 runner 模块的两个依赖指向替身，并返回替身执行器。"""

    executor = _FakeExecutor(run_state)
    monkeypatch.setattr(runner_module, "get_conversation_run_executor", lambda: executor)
    monkeypatch.setattr(runner_module, "get_conversation_run_state_service", lambda: run_state)
    return executor


def test_run_child_claims_pending_run_before_start(monkeypatch) -> None:
    """child run 为 pending 时必须先被认领，再进入执行器（否则必然 not running）。"""

    run_state = _FakeRunStateService("pending")
    executor = _wire(monkeypatch, run_state)
    runner = ChildAgentRunner(run_agent=_noop_agent)

    result = runner.run_child(_child_profile(7))

    assert run_state.claims == [7]
    assert executor.started == [7]
    assert result.status == "completed"


def test_run_child_fails_without_starting_when_run_not_claimable(monkeypatch) -> None:
    """child run 已终态（无法认领）时必须收敛为 failed，且不得启动执行。"""

    run_state = _FakeRunStateService("cancelled")
    executor = _wire(monkeypatch, run_state)
    runner = ChildAgentRunner(run_agent=_noop_agent)

    result = runner.run_child(_child_profile(9))

    assert run_state.claims == [9]
    assert executor.started == []
    assert result.status == "failed"
    assert "not claimable" in (result.error or "")
