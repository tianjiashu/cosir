"""``AgentRuntime.run_agent`` 异常分支终态落定竞态的单元测试（审查报告 #8 回归护栏）。

覆盖报告指出的核心竞态：用户取消后 turn 已被条件置为 ``cancelled``，若执行链随后
有异常冒泡进入 ``except Exception``，**不得**把 ``cancelled`` 覆写为 ``failed``。

修复后契约（2026-08-17）：

- ``except Exception`` 分支改用条件更新 ``fail_turn_if_running``（底层
  ``WHERE status=='running'``），仅当 turn 仍处于 running 时才落定 failed。
- 未命中（turn 已是 cancelled / completed 等终态）时跳过 ``RUN_FAILED`` 投影，不覆写历史终态。
- 不再调用无条件 ``update_turn_status``。

通过 ``object.__new__`` 绕过 ``AgentRuntime.__init__`` 的 service 单例装配，注入 mock
``TurnService`` / ``TaskService`` / ``WorkspaceService``，并让 ``_emit`` 在 ``RUN_STARTED``
处抛异常以进入异常分支（避免构造完整 workflow 链路）。
"""

from collections.abc import AsyncGenerator, Awaitable, Callable
from unittest.mock import MagicMock, patch

from app.core.agents.agent_profile import AgentProfile
from app.core.runtime.runner import AgentRuntime
from app.models.enums.event_type import EventType
from app.models.event.runtime_event import RuntimeEvent


def _make_runner(
    turn_service: MagicMock,
    emit: Callable[[RuntimeEvent, AgentProfile], Awaitable[RuntimeEvent]],
) -> AgentRuntime:
    """构造绕过 __init__ 的 AgentRuntime，注入 mock 服务与自定义 _emit。

    参数:
        turn_service: mock ``TurnService``，其 ``fail_turn_if_running`` 返回由用例控制。
        emit: 异步 ``_emit`` 替代实现，用于在 RUN_STARTED 注入异常并收集事件。

    返回:
        仅具备被测方法所需属性/方法的 AgentRuntime 实例。
    """

    runner = object.__new__(AgentRuntime)
    runner._turn_service = turn_service
    runner._task_service = MagicMock()
    runner._workspace_service = MagicMock()
    runner._emit = emit  # type: ignore[method-assign, assignment]
    return runner


def _make_agent(turn: MagicMock) -> AgentProfile:
    """构造测试用 AgentProfile（绕过 dataclass ``__init__``，不构建 workflow）。"""

    agent = object.__new__(AgentProfile)
    agent.turn = turn
    agent.agent_id = "developer"
    agent.main_agent = True
    agent.runtime_event_loop = None
    return agent


def _make_task_and_workspace(runner: AgentRuntime) -> None:
    """mock task/workspace 查询：main_agent 由 parent_task_id is None 推导。"""

    task = MagicMock()
    task.parent_task_id = None
    task.workspace_id = "ws-1"
    runner._task_service.get_task.return_value = task  # type: ignore[attr-defined]
    runner._workspace_service.get_workspace.return_value = MagicMock()  # type: ignore[attr-defined]


async def _collect(agen: AsyncGenerator[RuntimeEvent, None]) -> list[RuntimeEvent]:
    """消费异步生成器并收集全部事件。"""

    return [event async for event in agen]


def _emit_raising_on_started(
    emitted: list[RuntimeEvent],
) -> Callable[[RuntimeEvent, AgentProfile], Awaitable[RuntimeEvent]]:
    """构造 _emit：RUN_STARTED 时抛异常进入异常分支，其余事件正常收集。"""

    async def _emit(event: RuntimeEvent, agent: AgentProfile) -> RuntimeEvent:
        if event.event_type == EventType.RUN_STARTED:
            raise RuntimeError("boom during run start")
        emitted.append(event)
        return event

    return _emit


def _patch_hooks() -> None:
    """把 HookInterceptor / HookContext 替换为空实现，避免真实 hook 干扰。"""

    hook_interceptor = patch("app.core.runtime.runner.HookInterceptor").start()
    hook_context = patch("app.core.runtime.runner.HookContext").start()

    async def _noop_fire(*args: object, **kwargs: object) -> None:
        """Hook 兜底：无订阅时不产生任何副作用。"""

        return None

    hook_interceptor.async_safe_fire = _noop_fire
    hook_context.from_locatable.return_value = MagicMock()


def _stop_patches() -> None:
    """停止 _patch_hooks 启动的 patch。"""

    patch.stopall()


class TestExceptionAfterCancelKeepsCancelledTerminal:
    """turn 已取消 + 异常冒泡：终态保持 cancelled，不覆写、不发 RUN_FAILED。"""

    @patch("app.core.runtime.runner.log")
    async def test_keeps_cancelled_and_skips_run_failed(self, log: MagicMock) -> None:
        turn_service = MagicMock()
        turn_service.fail_turn_if_running.return_value = None  # 模拟 turn 已非 running
        emitted: list[RuntimeEvent] = []
        runner = _make_runner(turn_service, _emit_raising_on_started(emitted))
        runner._mark_stable_file_changes = MagicMock()  # type: ignore[method-assign]
        _make_task_and_workspace(runner)
        _patch_hooks()
        try:
            turn = MagicMock()
            turn.turn_id = "t-turn"
            turn.task_id = "t-task"
            events = await _collect(runner.run_agent(_make_agent(turn)))
        finally:
            _stop_patches()

        # 终态不被覆写：不再有无条件 update_turn_status；条件更新确实被走查。
        turn_service.update_turn_status.assert_not_called()
        assert turn_service.fail_turn_if_running.call_count >= 1
        # 首次调用（except 分支）必须带 end_reason=None（区别于 finally 的 client_disconnected）。
        assert turn_service.fail_turn_if_running.call_args_list[0].kwargs == {"end_reason": None}
        # 不产出 RUN_FAILED，避免与 RUN_CANCELLED 重复投影。
        assert not any(e.event_type == EventType.RUN_FAILED for e in events)
        assert not any(e.event_type == EventType.RUN_FAILED for e in emitted)
        # 未命中时快照收口也不该执行（终态非由本路径落定）。
        runner._mark_stable_file_changes.assert_not_called()
        # 真实异常不得静默丢弃：降级 warning 留痕（事件名 + 堆栈）。
        log.warning.assert_called_once()
        assert log.warning.call_args.args[0] == "task_failed_after_terminal"
        assert log.warning.call_args.kwargs.get("exc_info") is True


class TestExceptionOnRunningTurnMarksFailed:
    """turn 仍 running + 异常冒泡：条件落定 failed 并发 RUN_FAILED。"""

    @patch("app.core.runtime.runner.log")
    async def test_marks_failed_and_emits_run_failed(self, log: MagicMock) -> None:
        turn_service = MagicMock()
        # 第一次（except 分支）命中 running→failed；第二次（finally 断开兜底）已非 running。
        turn_service.fail_turn_if_running.side_effect = [MagicMock(), None]
        emitted: list[RuntimeEvent] = []
        runner = _make_runner(turn_service, _emit_raising_on_started(emitted))
        runner._mark_stable_file_changes = MagicMock()  # type: ignore[method-assign]
        _make_task_and_workspace(runner)
        _patch_hooks()
        try:
            turn = MagicMock()
            turn.turn_id = "t-turn"
            turn.task_id = "t-task"
            events = await _collect(runner.run_agent(_make_agent(turn)))
        finally:
            _stop_patches()

        # 走条件更新而非无条件覆写。
        turn_service.update_turn_status.assert_not_called()
        assert turn_service.fail_turn_if_running.call_count >= 1
        # 首次调用（except 分支）必须带 end_reason=None（区别于 finally 的 client_disconnected）。
        assert turn_service.fail_turn_if_running.call_args_list[0].kwargs == {"end_reason": None}
        # 命中时产出 RUN_FAILED，快照收口执行。
        assert any(e.event_type == EventType.RUN_FAILED for e in events)
        assert any(e.event_type == EventType.RUN_FAILED for e in emitted)
        runner._mark_stable_file_changes.assert_called()
        # 落定 failed 走 task_failed error 日志，不经 warning 留痕分支。
        log.warning.assert_not_called()
        log.exception.assert_called_once()
