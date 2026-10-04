"""子 Agent 配置提案工具与按需 Transport 命令的契约测试。"""

from app.assistant_transport.request import AssistantTransportRequest
from app.core.agents.define_agents import main_agent
from app.core.tools.tool_handler.child_task.propose_agent_configuration import (
    ProposeAgentConfigurationTool,
)
from app.models import ConversationRunExtra


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
