"""子 Agent 配置提案工具的生成契约测试。"""

from app.core.agents.define_agents import main_agent
from app.core.tool.multi_agent.propose_agent_configuration import ProposeAgentConfigurationTool


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
