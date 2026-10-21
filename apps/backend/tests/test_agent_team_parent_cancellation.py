"""Agent Team 节点经调度入口启动；Coordinator 等待节点释放时检查父 Task 的停止信号。"""

import asyncio
import threading
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from app.agent_team import coordinator as coordinator_module
from app.agent_team.coordinator import AgentTeamCoordinator
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.models.enums.agent_team_run_status import AgentTeamRunStatus

_PARENT_TASK = 9_200_001
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
    cancellation_registry.consume(_PARENT_TASK)


class _Node:
    """调度入口替身：节点执行在被取消前一直不释放。"""

    def __init__(self) -> None:
        self.cancelled: list[int] = []
        self._released = asyncio.Event()

    async def wait_released(self, _run_id: int) -> None:
        await self._released.wait()

    async def cancel(self, task_id: int) -> bool:
        self.cancelled.append(task_id)
        self._released.set()
        return True


def test_node_wait_stops_node_when_parent_is_cancelled(
    monkeypatch: pytest.MonkeyPatch, main_loop: asyncio.AbstractEventLoop
) -> None:
    node = _Node()
    monkeypatch.setattr(coordinator_module, "get_workflow_session", lambda: node)
    # 节点启动后父 Task 才被停止：等待释放的轮询应检出并经调度入口停止节点。
    threading.Timer(0.3, cancellation_registry.request_cancel, args=(_PARENT_TASK,)).start()

    AgentTeamCoordinator.__new__(AgentTeamCoordinator)._wait_node(
        main_loop, 5, _PARENT_TASK, _NODE_TASK
    )

    assert node.cancelled == [_NODE_TASK]


def test_team_is_cancelled_when_parent_task_is_stopped() -> None:
    updates: list[tuple[int, str, str | None]] = []
    coordinator = AgentTeamCoordinator.__new__(AgentTeamCoordinator)
    coordinator._team_run_crud = SimpleNamespace(
        update_status_if_in=lambda team_run_id, status, _sources, **kwargs: updates.append(
            (team_run_id, status, kwargs.get("end_reason"))
        )
    )
    team_run = SimpleNamespace(id=3, parent_task_id=_PARENT_TASK)

    assert coordinator._cancel_for_parent(team_run, SimpleNamespace()) is False
    cancellation_registry.request_cancel(_PARENT_TASK)
    try:
        assert coordinator._cancel_for_parent(team_run, SimpleNamespace()) is True
    finally:
        cancellation_registry.consume(_PARENT_TASK)
    assert [update[:2] for update in updates] == [(3, AgentTeamRunStatus.CANCELLED.value)]
