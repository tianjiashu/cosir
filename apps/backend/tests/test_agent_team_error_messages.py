import pytest
from pydantic import BaseModel, Field, ValidationError

from app.api.configuration.agent_team_error_messages import (
    agent_team_configuration_error_message,
)
from app.models.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration


class _TeamName(BaseModel):
    name: str = Field(min_length=1)


def test_maps_field_validation_to_a_chinese_editable_prompt() -> None:
    with pytest.raises(ValidationError) as captured:
        _TeamName(name="")

    assert agent_team_configuration_error_message(captured.value) == "Team 名称不能为空。"


def test_lists_every_graph_issue_with_its_node_instead_of_only_the_first() -> None:
    with pytest.raises(ValidationError) as captured:
        AgentTeamConfiguration.model_validate({
            "team_id": "review", "name": "Review", "description": "审查", "start_node_id": "review",
            "nodes": [
                {
                    "node_id": "review", "name": "Review", "agent_id": "reviewer",
                    "statuses": [
                        {"name": "done", "color": "#3B82F6"},
                        {"name": "needs_fix", "color": "#F59E0B"},
                    ],
                },
                {
                    "node_id": "fix", "name": "Fix", "agent_id": "fixer",
                    "statuses": [{"name": "done", "color": "#3B82F6"}],
                },
            ],
            "transitions": [{"from_node_id": "review", "status": "done", "target_node_id": "END"}],
        })

    assert agent_team_configuration_error_message(captured.value).split("\n") == [
        "节点“review”的状态“needs_fix”还没有设置下一步。",
        "节点“fix”的状态“done”还没有设置下一步。",
        "节点“fix”从入口出发走不到。",
        "节点“fix”之后无法走到 END。",
    ]
