"""Agent Team 在后台执行；Coordinator 以 TeamRun 自身状态判定取消，等待节点静止时检出即停止节点。"""

import asyncio
import threading
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from app.core.tool.handler.agent_team import coordinator as coordinator_module
from app.core.tool.handler.agent_team.coordinator import AgentTeamCoordinator
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


def test_node_wait_stops_node_when_team_run_is_cancelled(
    monkeypatch: pytest.MonkeyPatch, main_loop: asyncio.AbstractEventLoop
) -> None:
    node = _Node(main_loop)
    monkeypatch.setattr(
        coordinator_module.task_runtime_spaces,
        "get_or_create",
        lambda _task_id: SimpleNamespace(actor=node),
    )
    monkeypatch.setattr(coordinator_module.task_runtime_spaces, "cancel", node.cancel)
    runs = _TeamRuns(AgentTeamRunStatus.RUNNING.value)
    coordinator = AgentTeamCoordinator.__new__(AgentTeamCoordinator)
    coordinator._team_run_crud = runs
    # 节点启动后 TeamRun 才被取消：分段等待静止的间隙应检出并经调度入口停止节点。
    threading.Timer(0.3, setattr, args=(runs, "status", AgentTeamRunStatus.CANCELLED.value)).start()

    coordinator._wait_node(main_loop, _TEAM_RUN, _NODE_TASK)

    assert node.cancelled == [_NODE_TASK]


@pytest.mark.parametrize(
    ("status", "stopped"),
    [("running", False), ("cancelled", True), ("failed", True), ("completed", True)],
)
def test_coordinator_stops_once_team_run_leaves_running(status: str, stopped: bool) -> None:
    """取消以 TeamRun 状态为准：Team 后台执行时父 Run 可能挂起，没有可读的父 Task 信号。"""
    coordinator = AgentTeamCoordinator.__new__(AgentTeamCoordinator)
    coordinator._team_run_crud = _TeamRuns(status)

    assert coordinator._stopped(_TEAM_RUN) is stopped
