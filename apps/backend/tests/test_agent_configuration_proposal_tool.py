"""子 Agent 配置提案工具与按需 Transport 命令的契约测试。"""

import pytest

from app.agent_team.team_tool_error import TeamToolError
from app.assistant_transport.request import AssistantTransportRequest
from app.assistant_transport.request.assistant_transport_request import TransportRequestError
from app.core.agents.define_agents import main_agent
from app.core.tools.tool_handler.agent_team.propose_agent_team_configuration import (
    ProposeAgentTeamConfigurationTool,
)
from app.core.tools.tool_handler.child_task.propose_agent_configuration import (
    ProposeAgentConfigurationTool,
)
from app.models import ConversationRunExtra


def test_team_proposal_command_only_opens_team_draft_tool() -> None:
    """Team 草稿模式使用独立 Transport 标志。"""
    request = AssistantTransportRequest.model_validate(
        {
            "taskId": 1,
            "threadId": "task-1",
            "modelConfigId": 1,
            "commands": [
                {
                    "type": "add-message",
                    "commandId": "message",
                    "message": {"role": "user", "parts": [{"type": "text", "text": "生成 Team"}]},
                },
                {
                    "type": "custom",
                    "name": "propose-agent-team-configuration",
                    "commandId": "team-proposal",
                },
            ],
        }
    )
    assert request.commands[-1].name == "propose-agent-team-configuration"


def test_child_and_team_proposal_modes_are_mutually_exclusive() -> None:
    """Transport 边界拒绝绕过前端互斥选择器同时开启两种提案工具。"""
    with pytest.raises(TransportRequestError) as error:
        AssistantTransportRequest.model_validate(
            {
                "taskId": 1,
                "threadId": "task-1",
                "modelConfigId": 1,
                "commands": [
                    {
                        "type": "add-message",
                        "commandId": "message",
                        "message": {
                            "role": "user",
                            "parts": [{"type": "text", "text": "生成草稿"}],
                        },
                    },
                    {
                        "type": "custom",
                        "name": "propose-agent-configuration",
                        "commandId": "child",
                    },
                    {
                        "type": "custom",
                        "name": "propose-agent-team-configuration",
                        "commandId": "team",
                    },
                ],
            }
        )
    assert error.value.code == "AGENT_CONFIGURATION_PROPOSAL_COMMAND_INVALID"


def test_team_proposal_tool_returns_only_an_unsaved_configuration_draft(monkeypatch) -> None:
    """生成工具只投影 Team 配置草稿，不落盘或启动运行。"""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.service.agent_team.agent_team_preparation_service.resolve_node_profile",
        lambda *_args, **_kwargs: None,
    )
    proposal = {
        "team_id": "review_team",
        "name": "Review Team",
        "description": "Review changes",
        "start_node_id": "review",
        "nodes": [
            {
                "node_id": "review",
                "name": "Review",
                "agent_id": "reviewer",
                "statuses": ["done"],
            }
        ],
        "transitions": [{"from_node_id": "review", "status": "done", "target_node_id": "END"}],
    }
    observation = ProposeAgentTeamConfigurationTool().execute(
        execution_context=SimpleNamespace(workspace_root="."),
        **proposal,
    )

    assert observation.status == "success"
    assert observation.display_data["kind"] == "agent-team-configuration-draft"
    assert observation.display_data["configuration"]["team_id"] == "review_team"
    assert observation.display_data["configuration"]["scope"] == "workspace"


def test_team_proposal_reports_unavailable_agent_reference(monkeypatch) -> None:
    """节点 Agent 引用失败时保留领域错误信息，供模型修订草稿。"""
    from types import SimpleNamespace

    def reject_profile(*_args, **_kwargs):
        raise TeamToolError("未知 child Agent: reviewer")

    monkeypatch.setattr(
        "app.service.agent_team.agent_team_preparation_service.resolve_node_profile",
        reject_profile,
    )
    observation = ProposeAgentTeamConfigurationTool().execute(
        execution_context=SimpleNamespace(workspace_root="."),
        team_id="review_team",
        name="Review Team",
        description="Review changes",
        start_node_id="review",
        nodes=[
            {
                "node_id": "review",
                "name": "Review",
                "agent_id": "reviewer",
                "statuses": ["done"],
            }
        ],
        transitions=[{"from_node_id": "review", "status": "done", "target_node_id": "END"}],
    )

    assert observation.status == "error"
    assert observation.error == "agent_team_configuration_invalid"
    assert observation.reason == "未知 child Agent: reviewer"
    assert observation.retryable is False


def test_team_proposal_and_run_flags_round_trip_separately() -> None:
    extra = ConversationRunExtra(
        display_text="生成 Team 草稿",
        attachments=[],
        propose_agent_team_configuration=True,
    )
    assert ConversationRunExtra.from_dict(extra.to_dict()) == extra
    assert extra.to_dict()["propose_agent_team_configuration"] is True
    assert "propose_agent_configuration" not in extra.to_dict()


def test_proposal_tool_only_returns_unsaved_four_field_draft() -> None:
    """工具返回四个生成字段，并通过 display_data 暴露给只读 UI。"""

    observation = ProposeAgentConfigurationTool().execute(
        agent_id="reviewer",
        role="代码审查",
        description="审查变更",
        system_prompt="只审查代码并给出证据",
    )

    assert observation.status == "success"
    assert "只审查代码并给出证据" not in (observation.content or "")
    assert observation.display_data == {
        "kind": "agent-configuration-draft",
        "status": "draft",
        "agent_id": "reviewer",
        "role": "代码审查",
        "description": "审查变更",
        "system_prompt": "只审查代码并给出证据",
    }
    assert "propose_agent_configuration" not in main_agent().allowed_tools


def test_proposal_command_is_received_as_a_typed_transport_command() -> None:
    """配置提案命令参与请求解析，但不需要额外的持久化身份。"""

    request = AssistantTransportRequest.model_validate(
        {
            "taskId": 1,
            "threadId": "task-1",
            "modelConfigId": 1,
            "commands": [
                {
                    "type": "add-message",
                    "commandId": "message",
                    "message": {
                        "role": "user",
                        "parts": [{"type": "text", "text": "生成配置"}],
                    },
                },
                {
                    "type": "custom",
                    "name": "propose-agent-configuration",
                    "commandId": "proposal",
                },
            ],
        }
    )

    assert request.commands[-1].name == "propose-agent-configuration"


def test_proposal_run_extra_round_trips_without_version_field() -> None:
    """临时 Run 标记可重建，且不引入 version/compatibility 字段。"""

    extra = ConversationRunExtra(
        display_text="生成一个审查 Agent",
        attachments=[],
        propose_agent_configuration=True,
    )

    serialized = extra.to_dict()
    assert "version" not in serialized
    assert ConversationRunExtra.from_dict(serialized) == extra
