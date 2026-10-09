"""Agent Team 预览展示契约与结果投影测试。"""

from dataclasses import asdict
from types import SimpleNamespace

from app.agent_team.coordinator import AgentTeamCoordinator
from app.agent_team.state.agent_team_run_state import AgentTeamRunState
from app.api.agent_teams_api import _run_payload
from app.core.agents.model_settings import ModelSettings
from app.core.tools.display.agent_team_display import (
    build_agent_team_preview_display_data,
    build_agent_team_run_display_data,
)
from app.core.tools.tool_handler.agent_team.review_request import (
    AGENT_TEAM_REVIEW_DRAFT_SCHEMA,
    build_team_review_request,
)
from app.core.workflows.react.node_helper.user_input_projection import extract_requests
from app.service.agent_team.agent_team_preparation_service import (
    resolve_effective_model_settings,
)


def _preview_fields() -> dict[str, object]:
    """构造准备服务实际会给出的预览字段（含用户可编辑的目标与子目标）。"""

    return {
        "team_id": "code-quality",
        "name": "代码质量 Team",
        "goal": "交付报告",
        "node_goals": {"develop": "实现", "review": "审查"},
        "start_node": "develop",
        "nodes": [],
        "edges": [],
        "parent_task_id": 2,
        "parent_run_id": 3,
        "workspace_id": 1,
        "configuration": {"team_id": "code-quality"},
    }


def test_agent_team_preview_separates_display_from_review_request() -> None:
    """待确认事实挂在观察的专用字段上，展示数据只承载卡片内容。

    请求经 ``dataclasses.asdict`` 进 workflow state 后由 ``extract_requests`` 派生；展示数据里
    没有（也不应有）待决声明——那是工作流控制流事实，不是展示数据。
    """

    display_data = build_agent_team_preview_display_data(_preview_fields(), team_run_id=17)
    request = build_team_review_request(_preview_fields(), team_run_id=17)

    assert display_data["kind"] == "agent-team-preview"
    assert display_data["status"] == "pending"
    assert display_data["team_run_id"] == 17
    assert "requires_user_input" not in display_data
    assert "user_input_request" not in display_data

    assert request.kind == "agent_team_review"
    assert request.request_id == "17"
    assert [kind.value for kind in request.decisions] == ["approve", "reject"]
    assert request.draft_schema == AGENT_TEAM_REVIEW_DRAFT_SCHEMA
    assert request.draft == {
        "goal": "交付报告",
        "node_goals": {"develop": "实现", "review": "审查"},
        "configuration": {"team_id": "code-quality"},
    }

    observations = [
        {
            "tool_name": "agent_team",
            "status": "success",
            "content": "{}",
            "error": "",
            "reason": "",
            "retryable": False,
            "tool_call_id": "call-1",
            "display_data": display_data,
            # 工作流看到的形状就是 asdict 投影后的普通 dict。
            "user_input_request": asdict(request),
        }
    ]
    (derived,) = extract_requests(observations)
    assert derived.request_id == "17"
    assert [kind.value for kind in derived.decisions] == ["approve", "reject"]
    assert derived.draft == request.draft


def test_confirmed_team_display_data_stops_requesting_user_input() -> None:
    """启动分支的展示数据与观察都不再携带待决声明，否则会形成无法结束的挂起。"""

    display_data = build_agent_team_run_display_data(
        team_run_id=17,
        team_id="code-quality",
        goal="交付报告",
        node_goals={"develop": "实现"},
    )

    assert display_data["kind"] == "agent-team-preview"
    assert display_data["status"] == "running"
    assert "requires_user_input" not in display_data
    assert "user_input_request" not in display_data


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
    assert payload["node_results"] == []


def test_team_model_snapshot_falls_back_to_parent_materialized_settings() -> None:
    """没有独立 model_config 的子 Agent 使用主 Run 已物化模型并保留自身偏好。"""

    child_profile = SimpleNamespace(
        agent_id="general-assistant",
        model_config_id=None,
        model_settings=ModelSettings.default_settings(),
    )
    parent_materialized = ModelSettings(
        base_url="http://127.0.0.1/v1",
        api_key="secret",
        model_name="local-model",
        context_window_k=32,
        supports_thinking=True,
        supports_reasoning_effort=True,
        supports_image=False,
    )
    parent_profile = SimpleNamespace(
        model_settings=parent_materialized,
        model_config_id=None,
    )

    effective = resolve_effective_model_settings(child_profile, parent_profile)

    assert effective.model_name == "local-model"
    assert effective.base_url == parent_materialized.base_url
    assert effective.reasoning_effort == "high"
