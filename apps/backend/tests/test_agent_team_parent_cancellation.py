"""Team 的节点等待由 actor 事件唤醒；单项取消可以早于节点 Run 的创建。"""

import asyncio
from types import SimpleNamespace

import pytest

from app.core.tool.multi_agent.agent_team import team_coordinator as module
from app.models.agent_team.state.agent_team_run_components import AgentTeamNodeExecution
from app.models.enums.agent_team_run_end_reason import AgentTeamRunEndReason


@pytest.mark.parametrize("stop_before_node_starts", [True, False])
async def test_stop_waits_for_the_enqueued_node_then_cancels_it_once(
    monkeypatch, stop_before_node_starts
):
    current = None
    observed = asyncio.Event()
    asked = []
    execution = module._TeamExecution(1, "call-team")
    visit = AgentTeamNodeExecution(node_id="review", task_id=2, conversation_run_ids=[7])

    class Node:
        state_changed = asyncio.Event()
        stopped = asyncio.Event()

        async def wait_quiescent(self, _timeout):
            observed.set()
            return bool(asked)

        async def cancel(self, reason):
            asked.append((current.id, reason))

        def changed(self):
            previous, self.state_changed = self.state_changed, asyncio.Event()
            previous.set()

    node = Node()
    coordinator = module.AgentTeamCoordinator.__new__(module.AgentTeamCoordinator)
    coordinator._closing = False
    coordinator._task_service = SimpleNamespace(require_current_run=lambda _: current)
    coordinator._persist_running = lambda *_: True
    monkeypatch.setattr(
        module.task_runtime_spaces, "get_or_create", lambda _: SimpleNamespace(actor=node)
    )
    wait = asyncio.create_task(coordinator._await_node_run(5, object(), visit, execution))
    async with asyncio.timeout(3):
        await observed.wait()
        if stop_before_node_starts:
            execution.request_stop(AgentTeamRunEndReason.CANCELLED, silent=False)
        assert not asked
        observed.clear()
        current = SimpleNamespace(id=8)
        node.changed()
        if not stop_before_node_starts:
            await observed.wait()
            execution.request_stop(AgentTeamRunEndReason.CANCELLED, silent=False)
        assert await wait == 8
    assert visit.conversation_run_ids == [7, 8]
    assert asked == [(8, "parent")]
