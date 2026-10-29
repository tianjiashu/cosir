"""Coordinator 以 TeamRun 自身状态判定取消（取消本次 ``agent_team`` 调用的工具级信号先落定为 TeamRun
状态），等待节点落定时检出即停止节点。"""

import asyncio
import threading
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from app.core.tool.handler.agent_team import coordinator as coordinator_module
from app.core.tool.handler.agent_team.coordinator import AgentTeamCoordinator
from app.models.agent_team.state.agent_team_run_state import AgentTeamNodeExecution
from app.core.workflow.cancel.tool_call_cancellation_registry import (
    tool_call_cancellation_registry,
)
from app.models.enums.agent_team_run_status import AgentTeamRunStatus

_TEAM_RUN = 3
_NODE_TASK = 9_200_002
_PARENT_TASK = 9_200_001
_TOOL_CALL = "call_team"


@pytest.fixture
def main_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """模拟主事件循环：节点 Run 在它上面执行，Coordinator 在测试线程里等待。"""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()


class _Node:
    """节点 actor 与取消信号替身：节点 Task 在被取消前一直不落定。"""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.cancelled: list[int] = []
        self._loop = loop
        self._released = asyncio.Event()

    async def wait_quiescent(self, wait_seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._released.wait(), wait_seconds)
        except TimeoutError:
            return False
        return True

    def request_cancel(self, reason: str) -> None:
        # Coordinator 在工作线程里经节点 actor 发起停止；节点随之落定。
        assert reason == "parent"
        self.cancelled.append(_NODE_TASK)
        self._loop.call_soon_threadsafe(self._released.set)


class _TeamRuns:
    """TeamRun CRUD 替身：状态可被另一线程改写，模拟父 Agent 停止主 Task 时取消 Team。"""

    def __init__(self, status: str) -> None:
        self.status = status

    def get_by_id(self, _team_run_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            id=_TEAM_RUN,
            status=self.status,
            parent_task_id=_PARENT_TASK,
            tool_call_id=_TOOL_CALL,
        )


def _waiting_coordinator(
    monkeypatch: pytest.MonkeyPatch, node: _Node, runs: _TeamRuns, current_run_ids: list[int | None]
) -> AgentTeamCoordinator:
    """节点 Task 的当前 Run 依次取 ``current_run_ids``（取完后停在最后一个），模拟新 Run 晚到。"""

    monkeypatch.setattr(
        coordinator_module.task_runtime_spaces,
        "get_or_create",
        lambda _task_id: SimpleNamespace(actor=node),
    )

    def get_task(_task_id: int) -> SimpleNamespace:
        run_id = current_run_ids.pop(0) if len(current_run_ids) > 1 else current_run_ids[0]
        return SimpleNamespace(current_run_id=run_id)

    coordinator = AgentTeamCoordinator.__new__(AgentTeamCoordinator)
    coordinator._team_run_crud = runs
    coordinator._task_service = SimpleNamespace(get_task=get_task)
    coordinator._persist_running = lambda *_args: True
    return coordinator


def test_node_wait_stops_node_when_team_run_is_cancelled(
    monkeypatch: pytest.MonkeyPatch, main_loop: asyncio.AbstractEventLoop
) -> None:
    node = _Node(main_loop)
    runs = _TeamRuns(AgentTeamRunStatus.RUNNING.value)
    coordinator = _waiting_coordinator(monkeypatch, node, runs, [None, 101])
    execution = AgentTeamNodeExecution(node_id="develop", task_id=_NODE_TASK)
    # 节点启动后 TeamRun 才被取消：等待循环应检出并经调度入口停止节点。
    threading.Timer(0.3, setattr, args=(runs, "status", AgentTeamRunStatus.CANCELLED.value)).start()

    run_id = coordinator._await_node_run(main_loop, _TEAM_RUN, object(), execution)

    assert run_id == 101
    assert execution.conversation_run_ids == [101]
    assert node.cancelled == [_NODE_TASK]


def test_node_wait_defers_cancel_until_node_run_appears(
    monkeypatch: pytest.MonkeyPatch, main_loop: asyncio.AbstractEventLoop
) -> None:
    """Team 在节点 Run 开出来之前就被取消：取消要等看到新 Run 后再发，否则会落空。"""

    node = _Node(main_loop)
    runs = _TeamRuns(AgentTeamRunStatus.CANCELLED.value)
    coordinator = _waiting_coordinator(monkeypatch, node, runs, [7, 7, 7, 101])
    seen_before_cancel: list[list[int]] = []
    original_cancel = node.request_cancel

    def request_cancel(reason: str) -> None:
        seen_before_cancel.append(list(execution.conversation_run_ids))
        original_cancel(reason)

    monkeypatch.setattr(node, "request_cancel", request_cancel)
    # 节点上一次执行的 Run 7 已在时间线里：它不是这次输入的 Run。
    execution = AgentTeamNodeExecution(
        node_id="develop", task_id=_NODE_TASK, conversation_run_ids=[7]
    )

    run_id = coordinator._await_node_run(main_loop, _TEAM_RUN, object(), execution)

    assert run_id == 101
    assert seen_before_cancel == [[7, 101]]


@pytest.mark.parametrize(
    ("status", "stopped"),
    [("running", False), ("cancelled", True), ("failed", True), ("completed", True)],
)
def test_coordinator_stops_once_team_run_leaves_running(status: str, stopped: bool) -> None:
    """取消以 TeamRun 状态为准：Team 后台执行时父 Run 可能挂起，没有可读的父 Task 信号。"""
    coordinator = AgentTeamCoordinator.__new__(AgentTeamCoordinator)
    coordinator._team_run_crud = _TeamRuns(status)

    assert coordinator._stopped(_TEAM_RUN) is stopped


def test_node_wait_rejects_settling_without_a_new_run(
    monkeypatch: pytest.MonkeyPatch, main_loop: asyncio.AbstractEventLoop
) -> None:
    """节点无事可做时最新 Run 仍是旧的：输入没有开出新 Run，调度违约，交给 ``start`` 收敛为失败。"""
    node = _Node(main_loop)
    main_loop.call_soon_threadsafe(node._released.set)
    coordinator = _waiting_coordinator(
        monkeypatch, node, _TeamRuns(AgentTeamRunStatus.RUNNING.value), [7]
    )
    execution = AgentTeamNodeExecution(
        node_id="develop", task_id=_NODE_TASK, conversation_run_ids=[7]
    )

    with pytest.raises(RuntimeError):
        coordinator._await_node_run(main_loop, _TEAM_RUN, object(), execution)


def test_tool_call_cancel_signal_settles_team_run_and_stops_node(
    monkeypatch: pytest.MonkeyPatch, main_loop: asyncio.AbstractEventLoop
) -> None:
    """取消这次 ``agent_team`` 调用只打工具级信号：等待循环现读信号、落定 TeamRun 并停下节点。"""
    node = _Node(main_loop)
    runs = _TeamRuns(AgentTeamRunStatus.RUNNING.value)
    coordinator = _waiting_coordinator(monkeypatch, node, runs, [None, 101])
    cancelled_ids: list[int] = []

    def cancel(team_run_id: int) -> SimpleNamespace:
        cancelled_ids.append(team_run_id)
        runs.status = AgentTeamRunStatus.CANCELLED.value
        return runs.get_by_id(team_run_id)

    coordinator.cancel = cancel
    execution = AgentTeamNodeExecution(node_id="develop", task_id=_NODE_TASK)
    threading.Timer(
        0.3, tool_call_cancellation_registry.mark_cancelled, args=(_PARENT_TASK, _TOOL_CALL)
    ).start()
    try:
        run_id = coordinator._await_node_run(main_loop, _TEAM_RUN, object(), execution)
    finally:
        tool_call_cancellation_registry.clear(_PARENT_TASK, _TOOL_CALL)

    assert run_id == 101
    assert cancelled_ids == [_TEAM_RUN]
    assert node.cancelled == [_NODE_TASK]


def test_tool_call_cancel_signal_does_not_override_settled_team_run() -> None:
    """TeamRun 已落定时信号不再触发取消，按已有终态停止。"""
    coordinator = AgentTeamCoordinator.__new__(AgentTeamCoordinator)
    coordinator._team_run_crud = _TeamRuns(AgentTeamRunStatus.COMPLETED.value)
    coordinator.cancel = lambda _id: pytest.fail("已落定的 TeamRun 不应再取消")
    tool_call_cancellation_registry.mark_cancelled(_PARENT_TASK, _TOOL_CALL)
    try:
        assert coordinator._stopped(_TEAM_RUN) is True
    finally:
        tool_call_cancellation_registry.clear(_PARENT_TASK, _TOOL_CALL)
