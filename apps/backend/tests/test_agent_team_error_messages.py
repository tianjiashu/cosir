from pydantic import BaseModel, Field, ValidationError
import pytest

from app.api.configuration.agent_team_error_messages import (
    agent_team_configuration_error_message,
)


class _TeamName(BaseModel):
    name: str = Field(min_length=1)


def test_maps_field_validation_to_a_chinese_editable_prompt() -> None:
    with pytest.raises(ValidationError) as captured:
        _TeamName(name="")

    assert agent_team_configuration_error_message(captured.value) == "Team 名称不能为空。"


def test_maps_duplicate_status_transition_to_a_specific_prompt() -> None:
    error = ValueError("multiple ambiguous transitions for node/status: review/done")

    assert agent_team_configuration_error_message(error) == (
        "同一节点的同一业务状态只能设置一条转移，请合并或删除重复转移。"
    )
