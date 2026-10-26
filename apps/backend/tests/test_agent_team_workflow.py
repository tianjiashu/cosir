"""Agent Team 预览展示契约与结果投影测试。"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.service.agent_team.coordinator as coordinator_module
from app.service.agent_team.coordinator import AgentTeamCoordinator
from app.core.tool.handler.agent_team.team_tool_error import TeamToolError
from app.api.schemas.response.agent_team_run_response import AgentTeamRunResponse
from app.core.agents.model_settings import ModelSettings
from app.core.tool.handler.agent_team.agent_team_display import (
    build_agent_team_preview_display_data,
)
from app.models.enums.agent_team_run_end_reason import AgentTeamRunEndReason
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.service.agent_team.agent_team_preparation_service import (
    resolve_node_model,
)


def _preview_fields() -> dict[str, object]:
    """构造准备服务实际会给出的预览字段（含用户可编辑的目标与子目标）。"""

    return {
        "team_id": "code-quality",
        "name": "代码质量 Team",
        "goal": "交付报告",
        "node_goals": {
            "develop": {"goal": "实现", "summary": "实现"},
            "review": {"goal": "审查", "summary": "审查"},
        },
        "start_node": "develop",
        "nodes": [],
        "edges": [],
        "parent_task_id": 2,
        "parent_run_id": 3,
        "workspace_id": 1,
        "configuration": {"team_id": "code-quality"},
    }


def test_agent_team_preview_carries_only_card_content() -> None:
    """展示数据只承载卡片内容与 TeamRun 定位；「待确认」由 TeamRun 的 pending 状态表达。"""

    display_data = build_agent_team_preview_display_data(_preview_fields(), team_run_id=17)

    assert display_data["kind"] == "agent-team-approval"
    assert display_data["team_run_id"] == 17
    assert "requires_user_input" not in display_data
    assert "user_input_request" not in display_data


def test_cancelling_pending_team_run_persists_terminal_metadata(monkeypatch) -> None:
    """父 Run 取消时，pending TeamRun 也要原子收敛、写完整终态信息并落终态运行档案。"""

    row = SimpleNamespace(id=17, status=AgentTeamRunStatus.PENDING.value, state_json={})
    updated = SimpleNamespace(
        id=17,
        parent_run_id=23,
        status=AgentTeamRunStatus.CANCELLED.value,
    )
    captured: dict[str, object] = {}

    class _State:
        def running_node_task_id(self) -> int:
            return 41

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
    coordinator._run_archive_service = SimpleNamespace(
        write_terminal_archive=lambda run_id: captured.update(archived=run_id)
    )
    monkeypatch.setattr(
        coordinator_module,
        "AgentTeamRunState",
        SimpleNamespace(model_validate=lambda _value: _State()),
    )
    monkeypatch.setattr(
        coordinator_module.cancellation_registry,
        "request_cancel",
        lambda task_id, reason: captured.update(signalled=(task_id, reason)),
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
    assert captured["signalled"] == (41, "parent")
    assert captured["archived"] == 17


def test_team_run_view_is_typed_and_does_not_accept_runtime_state() -> None:
    """前端 TeamRun 契约只允许安全投影字段，不携带恢复执行快照。"""

    payload = {
        "id": 1,
        "team_id": "quality",
        "goal": "goal",
        "status": "running",
        "nodes": [{
            "node_id": "review", "name": "Review", "agent_id": "reviewer",
            "role": "审查", "goal_summary": "检查改动", "status": "running",
            "visits": [{
                "visit_number": 1, "conversation_run_id": 10, "status": "completed",
                "agents": [{
                    "task_id": 9, "title": "Review", "agent_id": "reviewer",
                    "role": "审查", "status": "completed",
                }],
            }],
        }],
        "current_node_id": "review",
    }
    view = AgentTeamRunResponse.model_validate(payload)
    assert view.nodes[0].visits[0].agents[0].task_id == 9
    assert "state" not in view.model_dump()
    # 恢复执行用的内部 state 不得经响应契约下发。
    with pytest.raises(ValidationError):
        AgentTeamRunResponse.model_validate({**payload, "state": {"node_runtime": {}}})


def test_team_node_model_falls_back_to_parent_model_config_and_keeps_own_preferences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有独立模型配置的节点：沿用主 Run 选用的模型配置，节点偏好覆盖、主 Agent 偏好补缺。"""
    from app.core.agents.resolved_model import ResolvedModel
    from app.models.model_config_record import ModelConfigRecord
    from app.service.agent_team import agent_team_preparation_service as preparation

    config = ModelConfigRecord(
        config_name="local",
        base_url="http://127.0.0.1/v1",
        api_key="secret",
        model_name="local-model",
        context_window_k=32,
        supports_thinking=True,
        supports_reasoning_effort=True,
        id=9,
    )
    monkeypatch.setattr(
        preparation,
        "get_model_config_service",
        lambda: SimpleNamespace(get_config=lambda config_id: config),
    )
    child_profile = SimpleNamespace(
        agent_id="general-assistant",
        model_config_id=None,
        model_settings=ModelSettings(reasoning_effort="high"),
    )
    parent_agent = SimpleNamespace(
        model=ResolvedModel(
            model_config_id=9,
            base_url=config.base_url,
            api_key=config.api_key,
            model_name=config.model_name,
            context_window_k=32,
            supports_thinking=True,
            supports_reasoning_effort=True,
            supports_image=False,
            temperature=0.3,
            reasoning_effort="low",
        )
    )

    node_model = resolve_node_model(child_profile, parent_agent)

    assert node_model.model_config_id == 9
    assert node_model.model_name == "local-model"
    assert node_model.reasoning_effort == "high"
    assert node_model.temperature == 0.3


def test_team_node_without_any_model_config_is_rejected() -> None:
    child_profile = SimpleNamespace(
        agent_id="general-assistant",
        model_config_id=None,
        model_settings=ModelSettings.default_settings(),
    )

    with pytest.raises(TeamToolError):
        resolve_node_model(child_profile, None)

