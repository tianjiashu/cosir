"""Agent Team 工具的前端展示数据投影。"""

from collections.abc import Mapping
from typing import Any, Literal

from app.core.tools.tool_models import ProposeAgentTeamConfigurationArgs


def build_agent_team_preview_display_data(
    preview_fields: Mapping[str, Any],
    *,
    team_run_id: int,
) -> dict[str, Any]:
    """从准备结果中投影供用户确认的 Team 预览数据。

    ``preview_fields`` 是 preparation service 生成的预览输入，不直接作为 Transport payload；
    本函数集中决定可进入 UI 的字段（目标、节点、边、配置与待确认运行标识）。

    「这张卡需要用户确认」不是展示事实：它由 ``agent_team`` 工具随观察下发的
    ``ToolObservation.user_input_request`` 表达（见 ``review_request``），投影给前端的载荷由
    ``ToolCallLifecycleManager.project_user_input_request`` 组装。本函数只负责卡片本身。
    """

    fields = (
        "team_id",
        "name",
        "goal",
        "node_goals",
        "start_node",
        "nodes",
        "edges",
        "parent_task_id",
        "parent_run_id",
        "workspace_id",
        "configuration",
    )
    return {
        "kind": "agent-team-preview",
        "status": "pending",
        **{key: preview_fields[key] for key in fields},
        "team_run_id": team_run_id,
    }


def build_agent_team_run_display_data(
    *,
    team_run_id: int,
    team_id: str,
    goal: str,
    node_goals: Mapping[str, str],
) -> dict[str, Any]:
    """投影「用户已批准、Team 已启动」的展示数据。

    复用 ``agent-team-preview`` 这个 ``kind``，使同一张卡片从「待确认」翻到「运行中」而不需要
    第二套前端渲染分支；``status`` 表达状态跃迁，进度细节由卡片按 ``team_run_id`` 查询运行状态。
    本函数的产物不携带待决请求声明，这是「用户已确认」在展示层的表现。
    """

    return {
        "kind": "agent-team-preview",
        "status": "running",
        "team_id": team_id,
        "team_run_id": team_run_id,
        "goal": goal,
        "node_goals": dict(node_goals),
    }


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
                "node_type": (
                    "start" if node.node_id == configuration.start_node_id else "middle"
                ),
                "statuses": node.statuses,
            }
            for node in configuration.nodes
        ],
        "edges": [transition.model_dump(mode="json") for transition in configuration.transitions],
        "configuration": document,
    }
