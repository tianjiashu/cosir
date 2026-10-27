"""Agent Team 在后台执行；Coordinator 以 TeamRun 自身状态判定取消，等待节点静止时检出即停止节点。"""

import asyncio
import threading
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from app.core.tool.handler.agent_team import coordinator as coordinator_module
from app.core.tool.handler.agent_team.coordinator import AgentTeamCoordinator
from app.models.agent_team.state.agent_team_run_state import AgentTeamNodeExecution
from app.models.enums.agent_team_run_status import AgentTeamRunStatus

_TEAM_RUN = 3
_NODE_TASK = 9_200_002


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
    """节点 actor 与取消信号替身：节点 Task 在被取消前一直不静止。"""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.cancelled: list[int] = []
        self._loop = loop
        self._released = asyncio.Event()

    async def wait_idle(self, wait_seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._released.wait(), wait_seconds)
        except TimeoutError:
            return False
        return True

    def cancel(self, task_id: int, reason: str) -> None:
        # Coordinator 在工作线程里经注册表发起停止节点 Task；节点随之静止。
        assert reason == "parent"
        self.cancelled.append(task_id)
        self._loop.call_soon_threadsafe(self._released.set)


class _TeamRuns:
    """TeamRun CRUD 替身：状态可被另一线程改写，模拟父 Agent 或用户取消 Team。"""

    def __init__(self, status: str) -> None:
        self.status = status

    def get_by_id(self, _team_run_id: int) -> SimpleNamespace:
        return SimpleNamespace(status=self.status)


def _waiting_coordinator(
    monkeypatch: pytest.MonkeyPatch, node: _Node, runs: _TeamRuns, current_run_ids: list[int | None]
) -> AgentTeamCoordinator:
    """节点 Task 的当前 Run 依次取 ``current_run_ids``（取完后停在最后一个），模拟新 Run 晚到。"""

    monkeypatch.setattr(
        coordinator_module.task_runtime_spaces,
        "get_or_create",
        lambda _task_id: SimpleNamespace(actor=node),
    )
    monkeypatch.setattr(coordinator_module.task_runtime_spaces, "cancel", node.cancel)

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

    run_id = coordinator._await_node_run(main_loop, _TEAM_RUN, object(), execution, None)

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
    original_cancel = node.cancel

    def cancel(task_id: int, reason: str) -> None:
        seen_before_cancel.append(list(execution.conversation_run_ids))
        original_cancel(task_id, reason)

    monkeypatch.setattr(coordinator_module.task_runtime_spaces, "cancel", cancel)
    execution = AgentTeamNodeExecution(node_id="develop", task_id=_NODE_TASK)

    run_id = coordinator._await_node_run(main_loop, _TEAM_RUN, object(), execution, 7)

    assert run_id == 101
    assert seen_before_cancel == [[101]]


@pytest.mark.parametrize(
    ("status", "stopped"),
    [("running", False), ("cancelled", True), ("failed", True), ("completed", True)],
)
def test_coordinator_stops_once_team_run_leaves_running(status: str, stopped: bool) -> None:
    """取消以 TeamRun 状态为准：Team 后台执行时父 Run 可能挂起，没有可读的父 Task 信号。"""
    coordinator = AgentTeamCoordinator.__new__(AgentTeamCoordinator)
    coordinator._team_run_crud = _TeamRuns(status)

    assert coordinator._stopped(_TEAM_RUN) is stopped
