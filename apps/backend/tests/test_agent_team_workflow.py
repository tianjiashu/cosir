"""Agent Team 主 Agent 挂起、恢复和结果投影测试。"""

import asyncio
from types import SimpleNamespace

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.agent_team.coordinator import AgentTeamCoordinator
from app.agent_team.preview_builder import resolve_effective_model_settings
from app.core.workflows.react.nodes.agent_team_confirmation_wait_node import (
    agent_team_confirmation_wait_node,
)
from app.api.agent_teams_api import _run_payload
from app.core.agents.model_settings import ModelSettings
from app.core.workflows.react.worflow_state.state import ReactGraphState


def _state() -> ReactGraphState:
    """构造等待节点所需的最小 graph state。"""

    return ReactGraphState(
        step_count=0,
        tool_error_count=0,
        requested_tool=False,
        final_response=False,
        terminal=False,
        max_steps=10,
        final_text="",
        last_tool_results={},
    )


def test_agent_team_wait_node_resumes_to_model_path() -> None:
    """等待节点初次执行保存断点，恢复后才离开节点。"""

    builder = StateGraph(ReactGraphState)
    builder.add_node("wait", agent_team_confirmation_wait_node)
    builder.add_node("finish", lambda _state: {"terminal": True})
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


def test_team_result_message_contains_node_results() -> None:
    """主 Agent 收到的 TeamResult 包含节点 status/output，而不是只收到终态。"""

    row = SimpleNamespace(
        team_run_id="run-1",
        team_id="quality",
        status="completed",
        goal_input="完成质量检查",
        current_node_id=None,
        current_node_status="passed",
        current_node_output="检查通过",
        state_json={
            "previous_outputs": [
                {"node_id": "review", "status": "passed", "output": "检查通过"}
            ]
        },
        failure_kind=None,
        failure_message=None,
    )

    result = AgentTeamCoordinator._build_team_result_message(row)

    assert '"status": "completed"' in result
    assert '"output": "检查通过"' in result


def test_team_run_payload_hides_runtime_profile_snapshots() -> None:
    """Team 状态查询不能把节点 system_prompt 暴露给前端。"""

    row = SimpleNamespace(
        team_run_id="run-1",
        team_id="quality",
        workspace_id=1,
        parent_task_id=2,
        parent_run_id=3,
        goal_input="goal",
        status="running",
        current_node_id="review",
        current_node_status="",
        current_node_output="",
        state_json={"node_runtime_snapshots": {"review": {"system_prompt": "secret"}}},
        failure_kind=None,
        failure_message=None,
        started_at=None,
        ended_at=None,
    )

    assert "node_runtime_snapshots" not in _run_payload(row)["state"]


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
