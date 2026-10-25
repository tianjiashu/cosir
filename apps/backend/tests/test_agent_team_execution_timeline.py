from types import SimpleNamespace

import pytest

from app.models.agent_team.state.agent_team_run_state import (
    AgentTeamNodeExecution,
    AgentTeamRunRuntime,
    AgentTeamRunState,
    AgentTeamTransitionRecord,
)
from app.service.agent_team import agent_team_execution_timeline
from app.service.agent_team.agent_team_execution_timeline import (
    project_node_visits,
)


class _FakeProfileRegistry:
    """按 agent_id 解析出确定性 role，便于断言 timeline 走注册表而非 Run 冻结字段。"""

    def resolve(self, workspace: str, agent_id: str) -> SimpleNamespace:
        return SimpleNamespace(role=f"role:{agent_id}")


@pytest.fixture
def fake_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agent_team_execution_timeline,
        "get_agent_profile_registry",
        lambda: _FakeProfileRegistry(),
    )


def _run(run_id: int, agent_id: str, status: str = "completed") -> SimpleNamespace:
    # 以 run_id 充当创建时间：子 Agent 归组只依赖 created_at 的相对顺序。
    return SimpleNamespace(
        id=run_id, status=status, final_output=None, agent_id=agent_id, created_at=run_id
    )


def test_visits_resolve_role_from_registry_by_agent_id(fake_registry: None) -> None:
    state = AgentTeamRunState(
        node_executions={
            "draft": AgentTeamNodeExecution(
                node_id="draft", task_id=100, conversation_run_ids=[10, 12]
            ),
            "verify": AgentTeamNodeExecution(
                node_id="verify", task_id=101, conversation_run_ids=[11, 13]
            ),
        },
        current_node_id="verify",
        node_runtime={
            "draft": AgentTeamRunRuntime(agent_id="planner", node_goal="起草目标", node_goal_summary="起草目标"),
            "verify": AgentTeamRunRuntime(agent_id="reviewer", node_goal="校验目标", node_goal_summary="校验目标"),
        },
        transition_history=[
            AgentTeamTransitionRecord(
                from_node_id="draft", status="ready", target_node_id="verify"
            ),
            AgentTeamTransitionRecord(
                from_node_id="verify", status="failed", target_node_id="draft"
            ),
            AgentTeamTransitionRecord(
                from_node_id="draft", status="ready", target_node_id="verify"
            ),
        ],
    )
    configuration = {
        "nodes": [
            {
                "node_id": "draft",
                "name": "起草",
                "agent_id": "planner",
                "statuses": [{"name": "ready", "color": "#16A34A"}],
            },
            {
                "node_id": "verify",
                "name": "校验",
                "agent_id": "reviewer",
                "statuses": [{"name": "failed", "color": "#DC2626"}],
            },
        ]
    }
    run_records = {
        10: _run(10, "planner"),
        11: _run(11, "reviewer"),
        12: _run(12, "planner"),
        13: _run(13, "reviewer", status="running"),
        201: _run(201, "writer"),
        202: _run(202, "writer"),
    }
    child_tasks_by_parent = {
        100: [
            SimpleNamespace(id=301, title="本轮起草助手", created_at=10.5, current_run_id=201),
            SimpleNamespace(id=302, title="返工起草助手", created_at=12.5, current_run_id=202),
        ]
    }

    visits_by_node = project_node_visits(
        state=state,
        configuration=configuration,
        team_status="running",
        run_records=run_records,
        child_tasks_by_parent=child_tasks_by_parent,
        workspace="system",
    )

    draft_visits = visits_by_node["draft"]
    assert [v["conversation_run_id"] for v in draft_visits] == [10, 12]
    # 角色来自注册表解析，而非 Run 上的冻结字段。
    assert draft_visits[0]["agents"][0]["agent_id"] == "planner"
    assert draft_visits[0]["agents"][0]["role"] == "role:planner"
    # 子 Agent 同样按 agent_id 解析角色。
    assert draft_visits[0]["agents"][1]["agent_id"] == "writer"
    assert draft_visits[0]["agents"][1]["role"] == "role:writer"
    # 子 Agent 按创建时间归入所在的那次节点访问。
    assert [a["task_id"] for a in draft_visits[0]["agents"][1:]] == [301]
    assert [a["task_id"] for a in draft_visits[1]["agents"][1:]] == [302]

    verify_visits = visits_by_node["verify"]
    assert verify_visits[0]["agents"][0]["role"] == "role:reviewer"


def test_running_placeholder_resolves_role_from_registry(fake_registry: None) -> None:
    state = AgentTeamRunState(
        current_node_id="draft",
        node_runtime={
            "draft": AgentTeamRunRuntime(agent_id="planner", node_goal="起草目标", node_goal_summary="起草目标"),
        },
    )

    visits_by_node = project_node_visits(
        state=state,
        configuration={
            "nodes": [
                {
                    "node_id": "draft",
                    "name": "起草",
                    "agent_id": "planner",
                    "statuses": [{"name": "ready", "color": "#16A34A"}],
                }
            ]
        },
        team_status="running",
        run_records={},
        child_tasks_by_parent={},
        workspace="system",
    )

    # 当前节点尚未创建 Run 时，占位访问的角色仍按节点 agent_id 从注册表解析。
    placeholder = visits_by_node["draft"][0]
    assert placeholder["conversation_run_id"] is None
    assert placeholder["status"] == "running"
    assert placeholder["agents"][0]["role"] == "role:planner"
