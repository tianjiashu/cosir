"""Agent Team 工具的前端展示数据投影。"""

from typing import Any, Literal

from app.core.tools.tool_models import ProposeAgentTeamConfigurationArgs


def build_agent_team_preview_display_data(preview: dict[str, Any]) -> dict[str, Any]:
    """返回已完成脱敏的 Team 预览展示数据。"""

    return {"kind": "agent-team-preview", **preview}


def build_agent_team_configuration_display_data(
    configuration: ProposeAgentTeamConfigurationArgs,
    *,
    scope: Literal["system", "workspace"],
) -> dict[str, Any]:
    """构造不包含 system prompt 和密钥的配置候选展示。

    参数:
        configuration: 已通过对应入口校验的提案模型或持久化配置模型。
        scope: 当前展示使用的目标作用域；提案阶段固定为 ``workspace``，不写入配置事实。

    返回:
        面向前端 Transport 的结构化展示数据。

    异常:
        KeyError 或 ValueError: 输入对象缺少预览所需字段时抛出，由工具层转换为工具错误。
    """

    document = configuration.model_dump(mode="json")
    document["scope"] = scope

    return {
        "kind": "agent-team-configuration-draft",
        "status": "draft",
        "team_id": configuration.team_id,
        "name": configuration.name,
        "description": configuration.description,
        "scope": scope,
        "start_node": configuration.start_node_id,
        "nodes": [
            {
                "node_id": node.node_id,
                "name": node.name,
                "agent_id": node.agent_id,
                "node_type": node.node_type,
                "statuses": node.statuses,
            }
            for node in configuration.nodes
        ],
        "edges": [transition.model_dump(mode="json") for transition in configuration.transitions],
        "configuration": document,
    }
