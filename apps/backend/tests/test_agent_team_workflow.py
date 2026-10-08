"""Agent Team 主 Agent 挂起、恢复和结果投影测试。"""

import asyncio
from types import SimpleNamespace

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.agent_team.coordinator import AgentTeamCoordinator
from app.agent_team.state.agent_team_run_state import AgentTeamRunState
from app.api.agent_teams_api import _run_payload
from app.core.agents.model_settings import ModelSettings
from app.core.workflows.react.nodes.user_input_wait_node import user_input_wait_node
from app.core.workflows.react.worflow_state.route import ReactRoute
from app.core.workflows.react.worflow_state.state import ReactGraphState
from app.service.agent_team.agent_team_preparation_service import (
    resolve_effective_model_settings,
)


def _state() -> ReactGraphState:
    """构造通用用户输入等待节点所需的最小 graph state。"""

    return ReactGraphState(
        step_count=0,
        tool_error_count=0,
        next_node=ReactRoute.MODEL,
        max_steps=10,
        final_text="",
        last_tool_results={
            "observations": [
                {
                    "status": "success",
                    "display_data": {
                        "requires_user_input": True,
                        "user_input_request": {
                            "kind": "agent_team_review",
                            "request_id": "1",
                        },
                    },
                }
            ]
        },
    )


def test_user_input_wait_node_resumes_to_model_path() -> None:
    """通用等待节点初次执行保存断点，恢复后才离开节点。"""

    builder = StateGraph(ReactGraphState)
    builder.add_node("wait", user_input_wait_node)
    builder.add_node("finish", lambda _state: {"next_node": ReactRoute.END})
    builder.add_edge(START, "wait")
    builder.add_edge("wait", "finish")
    builder.add_edge("finish", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "agent-team-wait"}}

    async def run() -> None:
        await graph.ainvoke(_state(), config)
        snapshot = await graph.aget_state(config)
        assert snapshot.next == ("wait",)
        await graph.ainvoke(Command(resume={"action": "team_completed"}), config)
        assert (await graph.aget_state(config)).next == ()

    asyncio.run(run())


def test_team_run_payload_hides_runtime_profile_snapshots() -> None:
    """Team 状态查询不能把节点 system_prompt 暴露给前端。"""

    row = SimpleNamespace(
        id=1,
        team_id="quality",
        workspace_id=1,
        parent_task_id=2,
        parent_run_id=3,
        goal_input="goal",
        status="running",
        state_json=AgentTeamRunState(
            node_runtime={"review": {"agent_id": "reviewer", "system_prompt": "secret"}}
        ).to_json(),
        end_reason=None,
        started_at=None,
        ended_at=None,
    )

    payload = _run_payload(row)
    assert "node_runtime" not in payload["state"]
    assert payload["active_node"] is None
    assert payload["node_results"] == []


def test_team_model_snapshot_falls_back_to_parent_materialized_settings() -> None:
    """没有独立 model_config 的子 Agent 使用主 Run 已物化模型并保留自身偏好。"""

    profile = SimpleNamespace(
        agent_id="general-assistant",
        model_settings=ModelSettings.default_settings(),
    )
    fallback = ModelSettings(
        base_url="http://127.0.0.1/v1",
        api_key="secret",
        model_name="local-model",
        context_window_k=32,
        supports_thinking=True,
        supports_reasoning_effort=True,
        supports_image=False,
    )

    effective = resolve_effective_model_settings(profile, fallback)

    assert effective.model_name == "local-model"
    assert effective.base_url == fallback.base_url
    assert effective.reasoning_effort == "high"
