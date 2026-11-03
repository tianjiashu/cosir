"""Agent Team 执行准备：把已解析的 Team 配置转换成一次运行的准备结果。

节点 Profile、工具名、模型设置与供审批卡展示的预览在同一次解析中生成。不保存配置文件、不创建
TeamRun、不推进节点执行：这些分别归配置服务、审批门（``agent_team_run_approval``）与 Coordinator。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config.configuration import get_agent_registry, get_tool_registry
from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.agent_runtime_config import AgentRuntimeConfig
from app.core.agents.model_settings import ModelSettingsError
from app.core.agents.resolved_model import ResolvedModel, resolve_model
from app.core.agents.structured_output_spec import StructuredOutputSpec
from app.core.tool.multi_agent.agent_team.team_tool_error import TeamToolError
from app.core.tool.multi_agent.args.node_goal import AgentTeamNodeGoal
from app.core.tool.multi_agent.derivation import child_depth
from app.core.tool.multi_agent.task_tool_scope import tools_for_task
from app.models.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.models.agent_team.configuration.team_node_definition import TeamNodeDefinition
from app.models.agent_team.configuration.team_transition_definition import END_TARGET_NODE_ID
from app.service.depends import get_model_config_service, get_task_service


@dataclass(frozen=True)
class AgentTeamPreparationResult:
    """一次 Agent Team 执行准备的不可变输出。

    ``preview_fields`` 是审批卡的展示内容，含供用户编辑后回传的静态配置；
    ``node_runtime_snapshots`` 面向确认后的节点执行。两者来自同一次解析，但确认时必须基于用户
    提交的配置重新生成，不能把展示数据当作执行事实。
    """

    preview_fields: dict[str, Any]
    node_runtime_snapshots: dict[str, dict[str, Any]]


def resolve_node_profile(workspace_root: str, agent_id: str) -> AgentProfile:
    """解析 Team 节点引用的 CHILD Agent Profile；配置提案与运行准备共用这一条可用性规则。

    异常:
        TeamToolError: 该 Agent 在工作区里不存在或不是 CHILD 类型。
    """

    profile = get_agent_registry().resolve(workspace_root, agent_id)
    if profile is None or profile.agent_type is not AgentProfileType.CHILD:
        raise TeamToolError(f"Team 节点引用的 child Agent 不可用: {agent_id}")
    return profile


def materialize_node_tools(profile: AgentProfile, *, depth: int) -> list[str]:
    """按节点 Agent Profile 固化本次运行的节点工具名清单；``depth`` 是节点 Task 的派生深度。

    节点工具 = Profile 允许的工具按 Team 节点（``agent_team_node``）的类型规则与深度过滤
    （``tools_for_task``）：节点在前端只读，去掉需要用户界面的工具；只允许委派这一种派生，去掉
    建 Team 与 fork；节点与主 Task 同层，主 Task 已到深度上限时委派也去掉。只做过滤、
    **不代为追加**。注册表里没有的名字原样保留：节点 Task 创建时解析 schema 会如实报错，不在这里
    悄悄丢掉。

    返回值是新列表：Profile 是注册表共享单例，**不得**就地修改，否则本次 Team 的工具集会污染
    同一进程后续所有 Agent。顺序沿用 Profile 的 ``allowed_tools``。
    """

    registry = get_tool_registry()
    definitions = [
        definition
        for name in profile.allowed_tools
        if (definition := registry.get_tool_definition(name)) is not None
    ]
    kept = {
        definition.name
        for definition in tools_for_task("agent_team_node", definitions, depth=depth)
    }
    registered = {definition.name for definition in definitions}
    return [name for name in profile.allowed_tools if name in kept or name not in registered]


def resolve_node_model(
        profile: AgentProfile,
        parent_agent: AgentRuntimeConfig | None,
) -> ResolvedModel:
    """解析节点确认时实际使用的完整模型，规则与主 Run 相同（``resolve_model``）。

    节点有独立模型配置时用它与节点偏好；没有时沿用主 Agent 本次 Run 选用的模型配置，节点偏好
    覆盖、主 Agent 生效的偏好补缺。冻结进快照的只有偏好与模型配置 ID，连接与能力字段在节点 Run
    装配时按其 ``model_config_id`` 重新解析（``api_key`` 不落盘）。

    异常:
        TeamToolError: 节点与主 Agent 都没有可用的模型配置，或该配置不完整、已不存在。
    """

    preferences = profile.model_settings
    model_config_id = profile.model_config_id
    if model_config_id is None and parent_agent is not None:
        model_config_id = parent_agent.model.model_config_id
        preferences = preferences.with_defaults(parent_agent.model.preferences())
    if model_config_id is None:
        raise TeamToolError(
            message=f"节点 Agent {profile.agent_id} 缺少可用的模型运行配置", retryable=False
        )
    try:
        return resolve_model(
            get_model_config_service().get_config(model_config_id), preferences
        )
    except (KeyError, ModelSettingsError) as exc:
        raise TeamToolError(
            message=f"节点 Agent {profile.agent_id} 缺少可用的模型运行配置", retryable=False
        ) from exc


def _node_routes(
        configuration: AgentTeamConfiguration, node: TeamNodeDefinition
) -> list[tuple[str, str]]:
    """当前节点每个状态 → 去向文案，按节点声明的状态顺序。

    图校验已保证每个状态恰有一条转移，这里直接取，不设缺失兜底；提示词与输出契约共用这份
    文案，两处说法不会分叉。
    """
    targets = {
        transition.status: transition.target_node_id
        for transition in configuration.transitions
        if transition.from_node_id == node.node_id
    }
    routes = []
    for status in node.statuses:
        target_node_id = targets[status.name]
        if target_node_id == END_TARGET_NODE_ID:
            destination = "end the Team"
        else:
            target = configuration.node(target_node_id)
            destination = f"hand off to {target.name} ({target.node_id})"
        routes.append((status.name, destination))
    return routes


def build_node_structured_output(
        configuration: AgentTeamConfiguration,
        node: TeamNodeDefinition,
) -> StructuredOutputSpec:
    """按当前节点允许的状态与去向生成严格输出契约；状态枚举只含本节点的状态。

    去向在 ``status`` 的说明里再写一遍：收尾时模型直接读的是这份 schema，不一定回看系统提示词。
    """
    routes = _node_routes(configuration, node)
    schema = {
        "type": "object",
        "description": (
            "Return this node's outcome and concise handoff information "
            "for downstream nodes."
        ),
        "properties": {
            "status": {
                "type": "string",
                "enum": [status for status, _ in routes],
                "description": (
                    "Choose one allowed business outcome. It determines the next node or ends "
                    "the Team; it is not a Run lifecycle status. Allowed routes: "
                    + "; ".join(f"{status} -> {destination}" for status, destination in routes)
                ),
            },
            "output": {
                "type": "string",
                "description": (
                    "This is your handoff channel to the next node: summarize your "
                    "conclusion and the essential information downstream nodes need."
                ),
                "maxLength": 100_000,
            },
        },
        "required": ["status", "output"],
        "additionalProperties": False,
    }
    return StructuredOutputSpec(name="agent_team_node_result", json_schema=schema)


def build_node_system_prompt(
        profile: AgentProfile,
        configuration: AgentTeamConfiguration,
        node: TeamNodeDefinition,
        goal: str,
        node_roles: dict[str, str],
) -> str:
    """在节点 Profile 提示词后追加 Team 身份与协议、总目标和当前节点的路由。

    总目标冻结在系统提示词里，节点子目标与上一节点交接走用户输入（Coordinator 组装）：前者对
    每个节点、每次执行都相同，后者随执行变化。只列当前节点的路由，不列全队流转图——其他节点的
    边对本节点没有决策价值，还会让模型误以为能选。JSON 交接格式不在这里写，由收尾的结构化输出
    节点带 schema 指令。不修改注册表里共享的 Profile。
    """
    members = "\n".join(
        f"- {item.name} ({item.node_id}): {node_roles[item.node_id]}"
        for item in configuration.nodes
    )
    routes = "\n".join(
        f"- `{status}`: {destination}" for status, destination in _node_routes(configuration, node)
    )
    team_context = "\n\n".join(
        (
            "## Agent Team\n"
            f'You are the node {node.name} ({node.node_id}) in Agent Team "{configuration.name}" '
            f"({configuration.team_id}). Nodes run one at a time:\n"
            "- Your context is your own: you see your subgoal and the previous node's handoff "
            "output, never other nodes' conversations.\n"
            "- The workspace file system is shared: files you change are what later nodes see.\n"
            "- When you finish, you return a status and an output. The output is the only thing "
            "the next node receives from you, so put your conclusions and everything it needs "
            "there; if your status ends the Team, your output is the Team's final result.\n\n"
            "Team members and roles:\n" + members,
            "## Overall Goal\n" + goal,
            "## Your Routes\nThe status you return decides what happens next:\n" + routes,
        )
    )
    base_prompt = profile.system_prompt.strip()
    return f"{base_prompt}\n\n{team_context}" if base_prompt else team_context


def prepare_team_run(
        configuration: AgentTeamConfiguration,
        *,
        goal: str,
        node_goals: dict[str, AgentTeamNodeGoal],
        workspace_root: str,
        parent_task_id: int,
        parent_agent: AgentRuntimeConfig,
) -> AgentTeamPreparationResult:
    """一次性解析 Team 节点，生成审批卡预览与节点运行快照；只读注册表与 Task，不写任何事实。

    ``goal`` 与 ``node_goals`` 的非空由两个入口参数模型保证（``AgentTeamArgs`` /
    ``AgentTeamApproveInput``），这里只去掉首尾空白；子目标是否与配置节点一一对应只有这里
    知道配置，在这里检查。``parent_agent`` 是节点缺少独立模型配置时回落的主 Agent 运行时配置。

    异常:
        TeamToolError: 子目标与配置节点不符（可重试），或节点 Agent / 模型配置不可用。
    """

    goal = goal.strip()
    node_ids = {node.node_id for node in configuration.nodes}
    node_goals = {
        key: value.model_copy(
            update={
                "goal": value.goal.strip(),
                "summary": value.summary.strip(),
            }
        )
        for key, value in node_goals.items()
    }
    unknown_node_goals = set(node_goals) - node_ids
    if unknown_node_goals:
        raise TeamToolError(
            message="node_goals references unknown nodes: "
            + ", ".join(sorted(unknown_node_goals)),
            retryable=True
        )
    missing_node_goals = node_ids - set(node_goals)
    if missing_node_goals:
        raise TeamToolError(
            message="node_goals must define every node: "
            + ", ".join(sorted(missing_node_goals)),
            retryable=True
        )
    # 节点与主 Task 同层（``child_depth`` 的 team 增量为 0），工具按这个深度固化。
    node_depth = child_depth(get_task_service().get_task(parent_task_id), "team")
    node_profiles = {
        node.node_id: resolve_node_profile(workspace_root, node.agent_id)
        for node in configuration.nodes
    }
    node_roles = {
        node_id: profile.role for node_id, profile in node_profiles.items()
    }

    resolved_nodes: list[dict[str, Any]] = []
    node_runtime_snapshots: dict[str, dict[str, Any]] = {}
    for node in configuration.nodes:
        profile = node_profiles[node.node_id]
        allowed_tools = materialize_node_tools(profile, depth=node_depth)
        node_model = resolve_node_model(profile, parent_agent)
        structured_output = build_node_structured_output(configuration, node)
        node_runtime_snapshots[node.node_id] = {
            "agent_id": profile.agent_id,
            "role": profile.role,
            "node_goal": node_goals[node.node_id].goal,
            "node_goal_summary": node_goals[node.node_id].summary,
            "system_prompt": build_node_system_prompt(
                profile,
                configuration,
                node,
                goal,
                node_roles,
            ),
            "allowed_tools": allowed_tools,
            "structured_output": structured_output.to_document(),
            "model_config_id": node_model.model_config_id,
            # 快照只冻结可持久化的请求偏好：连接与能力字段（尤其 ``api_key``）只在进程内
            # 存在，不得随快照落库；节点执行时按 ``run.model_config_id`` 重新解析
            # （``AgentRuntimeConfig.resolve``）。
            "model_settings": node_model.preferences().to_dict(),
        }
        resolved_nodes.append(
            {
                "node_id": node.node_id,
                "name": node.name,
                "agent_id": node.agent_id,
                "node_type": (
                    "start" if node.node_id == configuration.start_node_id else "middle"
                ),
                "role": profile.role,
                "model_config_id": node_model.model_config_id,
                "model_name": node_model.model_name,
                # 与 node_runtime_snapshots 用同一份工具名清单：预览展示的节点工具必须就是
                # 确认后冻结执行的那一份，否则用户看到的与真正执行的不一致。
                "tools": list(allowed_tools),
                "max_steps": profile.max_steps,
                "statuses": [status.model_dump(mode="json") for status in node.statuses],
            }
        )

    preview_fields = {
        "team_id": configuration.team_id,
        "name": configuration.name,
        "goal": goal,
        "node_goals": {
            node_id: node_goal.model_dump(mode="json")
            for node_id, node_goal in node_goals.items()
        },
        "start_node": configuration.start_node_id,
        "nodes": resolved_nodes,
        "edges": [
            transition.model_dump(mode="json") for transition in configuration.transitions
        ],
        # 配置本身不含密钥和 system prompt，前端可以基于它编辑最终提交内容；后端确认
        # 时仍会重新执行 AgentTeamConfiguration 和运行时资源校验。
        "configuration": configuration.model_dump(mode="json"),
    }
    return AgentTeamPreparationResult(
        preview_fields=preview_fields,
        node_runtime_snapshots=node_runtime_snapshots,
    )
