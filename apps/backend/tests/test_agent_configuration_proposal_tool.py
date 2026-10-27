"""子 Agent 配置提案工具的生成契约测试。"""

from app.core.tool.handler.agent_team.team_tool_error import TeamToolError
from app.core.agents.define_agents import main_agent
from app.core.tool.handler.propose_agent_team_configuration import (
    ProposeAgentTeamConfigurationTool,
)
from app.core.tool.handler.propose_agent_configuration import ProposeAgentConfigurationTool


def test_team_proposal_tool_returns_only_an_unsaved_configuration_draft(monkeypatch) -> None:
    """生成工具只投影 Team 配置草稿，不落盘或启动运行。"""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.core.tool.handler.agent_team.agent_team_preparation_service.resolve_node_profile",
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
                "statuses": [{"name": "done", "color": "#16A34A"}],
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
        "app.core.tool.handler.agent_team.agent_team_preparation_service.resolve_node_profile",
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
                "statuses": [{"name": "done", "color": "#16A34A"}],
            }
        ],
        transitions=[{"from_node_id": "review", "status": "done", "target_node_id": "END"}],
    )

    assert observation.status == "error"
    assert observation.content == "未知 child Agent: reviewer"


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
    assert "propose_agent_configuration" in main_agent().allowed_tools
    assert "propose_agent_team_configuration" in main_agent().allowed_tools
