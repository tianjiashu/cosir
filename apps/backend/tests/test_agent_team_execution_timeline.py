from types import SimpleNamespace

from app.agent_team.state.agent_team_run_state import (
    AgentTeamNodeExecution,
    AgentTeamRunRuntime,
    AgentTeamRunState,
    AgentTeamTransitionRecord,
)
from app.service.agent_team.agent_team_execution_timeline import (
    project_execution_timeline,
)


def test_execution_timeline_keeps_revisits_and_matches_child_agents_to_parent_run():
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
            "draft": AgentTeamRunRuntime(agent_id="planner", node_goal="起草目标"),
            "verify": AgentTeamRunRuntime(agent_id="reviewer", node_goal="校验目标"),
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
        run_id: SimpleNamespace(
            id=run_id,
            status="completed" if run_id != 13 else "running",
            final_output=None,
        )
        for run_id in (10, 11, 12, 13, 201, 202)
    }
    child_tasks_by_parent = {
        100: [
            SimpleNamespace(
                id=301,
                title="本轮起草助手",
                parent_run_id=10,
                current_run_id=201,
            ),
            SimpleNamespace(
                id=302,
                title="返工起草助手",
                parent_run_id=12,
                current_run_id=202,
            ),
        ]
    }

    steps = project_execution_timeline(
        state=state,
        configuration=configuration,
        team_status="running",
        run_records=run_records,
        child_tasks_by_parent=child_tasks_by_parent,
    )

    assert [step["node_id"] for step in steps] == ["draft", "verify", "draft", "verify"]
    assert [step["node_visit"] for step in steps] == [1, 1, 2, 2]
    assert [step["conversation_run_id"] for step in steps] == [10, 11, 12, 13]
    assert [step["business_status_color"] for step in steps] == [
        "#16A34A",
        "#DC2626",
        "#16A34A",
        None,
    ]
    assert steps[0]["child_agents"][0]["task_id"] == 301
    assert steps[2]["child_agents"][0]["task_id"] == 302
    assert steps[0]["goal"] == "起草目标"
    assert steps[3]["status"] == "running"
    assert steps[3]["transition_target_node_id"] is None


def test_running_timeline_shows_the_next_node_before_its_task_starts():
    state = AgentTeamRunState(
        current_node_id="draft",
        node_runtime={
            "draft": AgentTeamRunRuntime(agent_id="planner", node_goal="起草目标"),
        },
    )

    steps = project_execution_timeline(
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
    )

    assert steps == [{
        "step_number": 1,
        "node_visit": 1,
        "node_id": "draft",
        "name": "起草",
        "agent_id": "planner",
        "goal": "起草目标",
        "task_id": None,
        "conversation_run_id": None,
        "status": "queued",
        "business_status": None,
        "business_status_color": None,
        "transition_target_node_id": None,
        "child_agents": [],
    }]
