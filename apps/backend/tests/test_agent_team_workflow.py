"""Agent Team 预览展示契约与结果投影测试。"""

from dataclasses import asdict
from types import SimpleNamespace

import app.agent_team.coordinator as coordinator_module
from app.agent_team.coordinator import AgentTeamCoordinator
from app.api.schemas.response.agent_team_run_response import AgentTeamRunResponse
from app.core.agents.model_settings import ModelSettings
from app.core.tools.display.agent_team_display import (
    build_agent_team_preview_display_data,
    build_agent_team_run_display_data,
)
from app.core.tools.tool_handler.agent_team.review_request import (
    AGENT_TEAM_REVIEW_DRAFT_SCHEMA,
    build_team_review_request,
)
from app.core.workflows.react.node_helper.user_input_projection import extract_request
from app.models.enums.agent_team_run_end_reason import AgentTeamRunEndReason
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
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

    assert display_data["kind"] == "agent-team-approval"
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
    derived = extract_request(observations)
    assert derived is not None
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

    assert display_data["kind"] == "agent-team-execution"
    assert "requires_user_input" not in display_data
    assert "user_input_request" not in display_data


def test_cancelling_pending_team_run_persists_terminal_metadata(monkeypatch) -> None:
    """父 Run 取消时，pending TeamRun 也要原子收敛并写完整终态信息。"""

    row = SimpleNamespace(id=17, status=AgentTeamRunStatus.PENDING.value, state_json={})
    updated = SimpleNamespace(
        id=17,
        parent_run_id=23,
        status=AgentTeamRunStatus.CANCELLED.value,
    )
    captured: dict[str, object] = {}

    class _State:
        def cancel(self) -> None:
            captured["signalled"] = True

    class _Crud:
        def get_by_id(self, run_id: int) -> SimpleNamespace:
            assert run_id == 17
            return row

        def update_status_if_in(
            self,
            run_id: int,
            target: str,
            allowed: tuple[str, ...],
            **kwargs: object,
        ) -> SimpleNamespace:
            captured.update(run_id=run_id, target=target, allowed=allowed, **kwargs)
            return updated

    coordinator = AgentTeamCoordinator.__new__(AgentTeamCoordinator)
    coordinator._team_run_crud = _Crud()
    monkeypatch.setattr(
        coordinator_module,
        "AgentTeamRunState",
        SimpleNamespace(model_validate=lambda _value: _State()),
    )

    result = coordinator.cancel(17)

    assert result is updated
    assert captured["allowed"] == (
        AgentTeamRunStatus.RUNNING.value,
        AgentTeamRunStatus.PENDING.value,
    )
    assert captured["target"] == AgentTeamRunStatus.CANCELLED.value
    assert captured["end_reason"] == AgentTeamRunEndReason.CANCELLED.value
    assert captured["ended"] is True


def test_team_run_view_is_typed_and_does_not_accept_runtime_state() -> None:
    """前端 TeamRun 契约只允许安全投影字段，不携带恢复执行快照。"""

    payload = {
        "id": 1,
        "team_id": "quality",
        "workspace_id": 1,
        "parent_task_id": 2,
        "parent_run_id": 3,
        "goal": "goal",
        "status": "running",
        "start_node_id": "review",
        "nodes": [{
            "node_id": "review", "name": "Review", "agent_id": "reviewer",
            "goal": "review goal",
            "statuses": [],
            "task_id": 9, "status": "running", "conversation_run_count": 1,
            "agent_count": 1, "child_agents": [],
        }],
        "execution_steps": [],
        "transitions": [],
        "transition_history": [],
        "current_node_id": "review",
        "end_reason": None,
        "started_at": None,
        "ended_at": None,
    }
    view = AgentTeamRunResponse.model_validate(payload)
    assert view.nodes[0].task_id == 9
    assert "state" not in view.model_dump()


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
