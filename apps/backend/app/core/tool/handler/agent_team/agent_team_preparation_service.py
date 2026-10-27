"""Agent Team 执行准备服务。

本模块把已解析的 Team 配置转换成一次运行准备结果：节点 Profile、工具定义、模型设置、
供展示层投影的预览输入和预览指纹均在同一次解析中生成。它不保存配置文件，不创建 TeamRun，
也不推进节点执行；这些职责分别由配置服务和 Team coordinator 负责。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.models.agent_team.configuration.team_node_definition import TeamNodeDefinition
from app.models.agent_team.configuration.team_transition_definition import END_TARGET_NODE_ID
from app.core.tool.handler.agent_team.args.node_goal import AgentTeamNodeGoal
from app.core.tool.handler.agent_team.team_tool_error import TeamToolError
from app.config.configuration import get_agent_registry
from app.config.constant import Constant
from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.agent_runtime_config import AgentRuntimeConfig
from app.core.agents.model_settings import ModelSettingsError
from app.core.agents.resolved_model import ResolvedModel, resolve_model
from app.core.agents.structured_output_spec import StructuredOutputSpec
from app.service.depends import get_model_config_service


@dataclass(frozen=True)
class AgentTeamPreparationResult:
    """一次 Agent Team 执行准备的不可变输出。

    ``preview_fields`` 是交给展示投影函数的预览输入，包含供用户编辑后回传的静态配置；
    ``node_runtime_snapshots`` 面向确认后的节点执行。两者来自同一次解析，但最终确认时
    必须基于用户提交的配置重新生成，
    不能把展示数据直接当作执行事实。
    """

    preview_fields: dict[str, Any]
    node_runtime_snapshots: dict[str, dict[str, Any]]


def resolve_node_profile(
        configuration: Any,
        workspace_root: str,
        node_id: str,
) -> tuple[TeamNodeDefinition, Any]:
    """解析 Team 节点及其 CHILD Agent Profile。

    参数:
        configuration: 具有 ``nodes`` 集合的 Team 配置或配置候选对象。
        workspace_root: Profile 解析使用的 workspace 根路径。
        node_id: 要解析的节点标识。

    返回:
        配置节点和已注册的 CHILD Agent Profile。

    异常:
        KeyError: 节点不存在。
        TeamToolError: 节点引用的 Agent 不存在或不是 CHILD 类型。
    """

    node = next((item for item in configuration.nodes if item.node_id == node_id), None)
    if node is None:
        raise KeyError(node_id)
    profile = get_agent_registry().resolve(workspace_root, node.agent_id)
    if profile is None or profile.agent_type is not AgentProfileType.CHILD:
        raise TeamToolError(f"Team 节点引用的 child Agent 不可用: {node.agent_id}")
    return node, profile


def materialize_node_tools(profile: AgentProfile) -> list[str]:
    """按节点 Agent Profile 固化本次运行的节点工具名清单。

    节点工具 = Profile 允许的工具 **减去** :attr:`Constant.Tools.TEAM_NODE_DISALLOWED_TOOLS`
    （禁止节点递归建 Team、提案配置或占用可交互终端）。本清单只做剥离、**不代为追加**：委派与
    父子通信工具不在禁用清单内，Profile 声明了它们就照样保留——Team 节点需要把子工作再拆给子
    Agent 并汇总；而 ``delegate_task`` 产生的子 Agent 会被
    :attr:`Constant.Tools.CHILD_DISALLOWED_TOOLS` 剥掉这些工具，避免无限套娃。两条清单的差别
    就只有这一点，不要为「统一清单」而互相移植。

    返回值是新列表：Profile 是注册表共享单例，**不得**就地修改，否则本次 Team 的工具集会污染
    同一进程后续所有 Agent。

    参数:
        profile: 已解析的节点 Agent Profile（由 :func:`resolve_node_profile` 解析）。

    返回:
        本次运行冻结的节点工具名列表，顺序沿用 Profile 的 ``allowed_tools``。

    异常:
        无（节点工具可用性由本函数归一化；工具名合法性已由 Profile 加载期校验）。
    """

    return [
        name
        for name in profile.allowed_tools
        if name not in Constant.Tools.TEAM_NODE_DISALLOWED_TOOLS
    ]


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


class AgentTeamPreparationService:
    """把 Team 配置和当前运行时依赖准备成一次不可变执行计划。

    本服务无内部持久化和进程级缓存，可以按请求创建；调用期间会读取 Agent registry 和
    工具注册表，但只把解析结果写入返回值。执行请求 service 负责把结果写入持久化请求。
    """

    def prepare(
            self,
            configuration: AgentTeamConfiguration,
            *,
            goal: str,
            node_goals: dict[str, AgentTeamNodeGoal],
            workspace_root: str,
            parent_task_id: int,
            parent_run_id: int,
            workspace_id: int,
            parent_agent: AgentRuntimeConfig,
    ) -> AgentTeamPreparationResult:
        """一次性解析 Team 节点并生成展示预览、运行快照和指纹。

        参数:
            configuration: 已通过 Team 配置领域校验的配置对象。
            goal: 本次 Team 执行目标，不能为空白。
            node_goals: 按 ``node_id`` 提供且完整覆盖所有节点的子目标与展示摘要；只有完整子目标
                作为对应节点的 user input。
            workspace_root: 节点 Agent 和工具解析使用的 workspace 根路径。
            parent_task_id: 主 Agent Task 标识，写入展示预览。
            parent_run_id: 主 Agent Run 标识，写入展示预览。
            workspace_id: workspace 标识，写入展示预览。
            parent_agent: 节点缺少独立连接配置时使用的主 Agent 运行时配置。

        返回:
        同一次运行时解析产生的预览文档、节点运行快照和指纹。预览文档只供工具结果展示，
        不由本服务持久化。

        异常:
            TeamToolError: 总目标、节点子目标或节点运行时依赖无效。
    """

        goal = goal.strip()
        if not goal:
            raise TeamToolError(message="Team goal must not be blank", retryable=True)
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
                message = "node_goals references unknown nodes: "
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
        node_profiles: dict[str, AgentProfile] = {}
        for node in configuration.nodes:
            _, profile = resolve_node_profile(
                configuration,
                workspace_root,
                node.node_id,
            )
            node_profiles[node.node_id] = profile
        node_roles = {
            node_id: profile.role for node_id, profile in node_profiles.items()
        }

        resolved_nodes: list[dict[str, Any]] = []
        node_runtime_snapshots: dict[str, dict[str, Any]] = {}
        for node in configuration.nodes:
            profile = node_profiles[node.node_id]
            allowed_tools = materialize_node_tools(profile)
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
            "parent_task_id": parent_task_id,
            "parent_run_id": parent_run_id,
            "workspace_id": workspace_id,
            # 配置本身不含密钥和 system prompt，前端可以基于它编辑最终提交内容；后端确认
            # 时仍会重新执行 AgentTeamConfiguration 和运行时资源校验。
            "configuration": configuration.model_dump(mode="json"),
        }
        return AgentTeamPreparationResult(
            preview_fields=preview_fields,
            node_runtime_snapshots=node_runtime_snapshots,
        )
