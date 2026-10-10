"""Agent Team 执行准备服务。

本模块把已解析的 Team 配置转换成一次运行准备结果：节点 Profile、工具定义、模型设置、
供展示层投影的预览输入和预览指纹均在同一次解析中生成。它不保存配置文件，不创建 TeamRun，
也不推进节点执行；这些职责分别由配置服务和 Team coordinator 负责。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from app.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.agent_team.configuration.team_node_definition import TeamNodeDefinition
from app.agent_team.configuration.team_transition_definition import END_TARGET_NODE_ID
from app.agent_team.team_tool_error import TeamToolError
from app.config.configuration import get_agent_registry
from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.model_settings import ModelSettings, ModelSettingsError
from app.core.agents.structured_output_spec import StructuredOutputSpec
from app.core.tools.schemas.tool_names import *
from app.service.depends import get_model_config_service

TEAM_NODE_DISALLOWED_TOOLS = frozenset(
    {
        TOOL_AGENT_TEAM,
        TOOL_PROPOSE_AGENT_TEAM_CONFIGURATION,
        TOOL_PROPOSE_AGENT_CONFIGURATION,
        TOOL_TERMINAL_START,
        TOOL_TERMINAL_READ,
        TOOL_TERMINAL_WRITE,
        TOOL_TERMINAL_CLOSE,
        TOOL_TERMINAL_SIGNAL,
    }
)


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

    节点工具 = Profile 允许的工具 ∪ 子 Agent 协作工具（``delegate_task`` / ``child_agent_*``，
    节点需要把子工作再委派下去），再减去 :data:`TEAM_NODE_DISALLOWED_TOOLS`（Team 禁止节点
    递归建 Team、提案配置或占用可交互终端）。返回值是新列表：Profile 是注册表共享单例，
    **不得**就地 ``extend``，否则本次 Team 的工具集会污染同一进程后续所有 Agent。

    参数:
        profile: 已解析的节点 Agent Profile（由 :func:`resolve_node_profile` 解析）。

    返回:
        本次运行冻结的节点工具名列表，顺序为 Profile 原顺序 + 追加的协作工具。

    异常:
        无（节点工具可用性由本函数归一化；工具名合法性已由 Profile 加载期校验）。
    """

    allowed_tools = [
        name for name in profile.allowed_tools if name not in TEAM_NODE_DISALLOWED_TOOLS
    ]
    for name in (
        TOOL_DELEGATE_TASK,
        TOOL_CHILD_AGENT_SEND,
        TOOL_CHILD_AGENT_STATUS,
        TOOL_CHILD_AGENT_WAIT,
    ):
        if name not in allowed_tools:
            allowed_tools.append(name)
    return allowed_tools

def require_model_settings(model_settings: ModelSettings) -> ModelSettings | None:
    """断言模型设置已具备模型构建所需的物化字段。"""
    try:
        return model_settings.require_runtime_config()
    except ModelSettingsError:
        return None


def resolve_effective_model_settings(
        profile: AgentProfile,
        parent_agent_profile: AgentProfile | None,
) -> ModelSettings:
    """解析节点确认时实际使用的完整模型设置。

    子 Agent Profile 可能只有用户偏好而没有独立运行配置；此时沿用主 Agent 本次 Run 已
    物化的模型连接配置，并应用节点偏好覆盖。返回值会进入本次 Team 的冻结运行快照。

    异常:
        TeamToolError: Profile 和 fallback 都不能提供完整模型运行配置。
    """

    model_settings = require_model_settings(profile.model_settings)
    if model_settings is not None:
        return model_settings
    if parent_agent_profile is None:
        raise TeamToolError(message=f"节点 Agent {profile.agent_id} 缺少可用的模型运行配置", retryable=False) from None

    parent_settings = parent_agent_profile.model_settings
    parent_model_config_id = parent_agent_profile.model_config_id

    effective = parent_settings.with_preference_defaults(profile.model_settings)
    model_settings = require_model_settings(effective)
    if model_settings is not None:
        return model_settings
    model_config = get_model_config_service().get_config(profile.model_config_id if profile.model_config_id is not None else parent_model_config_id)
    if model_config is None:
        raise TeamToolError(message=f"节点 Agent {profile.agent_id} 缺少可用的模型运行配置", retryable=False) from None
    model_settings = profile.model_settings.from_model_config_record(model_config)
    if model_settings is not None:
        return model_settings
    raise TeamToolError(message=f"节点 Agent {profile.agent_id} 缺少可用的模型运行配置", retryable=False) from None


def build_node_structured_output(
        configuration: AgentTeamConfiguration,
        node: TeamNodeDefinition,
) -> StructuredOutputSpec:
    """按 Team 节点允许的状态和转移目标生成严格输出契约。

    参数:
        configuration: 本次运行冻结的 Team 工作流配置。
        node: 当前节点定义。

    返回:
        包含状态和下游结果的 JSON Schema 契约；状态枚举只包含当前节点允许的值。

    异常:
        TeamToolError: 节点状态没有对应转移时抛出。

    副作用:
        无；只读取配置并构造不可变输出契约。
    """

    node_names = {item.node_id: item.name for item in configuration.nodes}
    routes = {
        transition.status: transition.target_node_id
        for transition in configuration.transitions
        if transition.from_node_id == node.node_id
    }
    route_descriptions = []
    for status in node.statuses:
        target_node_id = routes.get(status)
        if target_node_id is None:
            raise TeamToolError(
                f"Team 节点状态缺少转移目标: {node.node_id}/{status}"
            )
        if target_node_id == END_TARGET_NODE_ID:
            destination = "End the Team"
        else:
            destination = f"{node_names[target_node_id]} ({target_node_id})"
        route_descriptions.append(f"{status} -> {destination}")

    schema = {
        "type": "object",
        "description": (
            "Return this node's outcome and concise handoff information "
            "for downstream nodes."
        ),
        "properties": {
            "status": {
                "type": "string",
                "enum": node.statuses,
                "description": (
                        "Choose one allowed business outcome. It determines the next node or ends the Team; "
                        "it is not a Run lifecycle status. Allowed routes: "
                        + "; ".join(route_descriptions)
                ),
            },
            "output": {
                "type": "string",
                "description": (
                    "Summarize this node's conclusion and the essential information "
                    "downstream nodes need."
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
    """在节点 Profile 提示词后追加总目标、全队角色概览和流转协议。

    参数:
        profile: 已解析的节点 Agent Profile；本函数不修改该对象。
        configuration: 本次运行冻结的 Team 工作流配置。
        node: 当前节点定义。
        goal: 本次运行的全局目标，确认后冻结在每个节点的 system prompt 中。
        node_roles: 本次运行中每个节点 Agent Profile 声明的职责，用于帮助节点理解全队分工。

    返回:
        原 Profile 提示词和 Team 专属说明组成的新提示词。

    异常:
        无。

    副作用:
        无；不会修改注册表中的 Agent Profile。
    """

    route_lines = []
    for transition in configuration.transitions:
        if transition.from_node_id != node.node_id:
            continue
        if transition.target_node_id == END_TARGET_NODE_ID:
            route_lines.append(
                f"- On status '{transition.status}', end the Agent Team."
            )
            continue
        target = configuration.node(transition.target_node_id)
        route_lines.append(
            f"- On status '{transition.status}', hand off to {target.name} ({target.node_id})."
        )

    workflow_nodes = [
        f"- {item.name} ({item.node_id}); Agent role: {node_roles[item.node_id]}"
        for item in configuration.nodes
    ]
    workflow_transitions = []
    for item in configuration.transitions:
        source_name = configuration.node(item.from_node_id).name
        if item.target_node_id == END_TARGET_NODE_ID:
            target_label = "End the Agent Team"
        else:
            target = configuration.node(item.target_node_id)
            target_label = f"{target.name} ({target.node_id})"
        workflow_transitions.append(
            f"- {source_name} ({item.from_node_id}) --{item.status}--> {target_label}"
        )
    team_context = "\n".join(
        (
            "## Agent Team Execution Protocol",
            "## Overall Team Goal\n" + goal,
            f'You are working in Agent Team "{configuration.name}" ({configuration.team_id}).',
            f"Current node: {node.name} ({node.node_id}).",
            "Team members and roles:\n" + "\n".join(workflow_nodes),
            "Team workflow transitions:\n"
            + ("\n".join(workflow_transitions) or "- No explicit transitions."),
            "Current node outcomes and routing:\n"
            + ("\n".join(route_lines) or "- The Team ends after this node completes."),
        )
    )
    base_prompt = profile.system_prompt.strip()
    return f"{base_prompt}\n\n{team_context}" if base_prompt else team_context


class AgentTeamPreparationService:
    """把 Team 配置和当前运行时依赖准备成一次不可变执行计划。

    本服务无内部持久化和进程级缓存，可以按请求创建；调用期间会读取 Agent registry 和
    ToolSystem，但只把解析结果写入返回值。执行请求 service 负责把结果写入持久化请求。
    """

    def prepare(
            self,
            configuration: AgentTeamConfiguration,
            *,
            goal: str,
            node_goals: dict[str, str],
            workspace_root: str,
            parent_task_id: int,
            parent_run_id: int,
            workspace_id: int,
            parent_agent_profile: AgentProfile,
    ) -> AgentTeamPreparationResult:
        """一次性解析 Team 节点并生成展示预览、运行快照和指纹。

        参数:
            configuration: 已通过 Team 配置领域校验的配置对象。
            goal: 本次 Team 执行目标，不能为空白。
            node_goals: 按 ``node_id`` 提供且完整覆盖所有节点的子目标，作为对应节点的 user input。
            workspace_root: 节点 Agent 和工具解析使用的 workspace 根路径。
            parent_task_id: 主 Agent Task 标识，写入展示预览。
            parent_run_id: 主 Agent Run 标识，写入展示预览。
            workspace_id: workspace 标识，写入展示预览。
            parent_agent_profile: 节点缺少独立连接配置时使用的主 Agent Profile。

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
            key: value.strip() for key, value in node_goals.items()
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
        blank_node_goals = sorted(
            node_id for node_id, value in node_goals.items() if not value
        )
        if blank_node_goals:
            raise TeamToolError(
                message="node_goals must not be blank: " + ", ".join(blank_node_goals),
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
            model_settings: ModelSettings = resolve_effective_model_settings(profile, parent_agent_profile)
            structured_output = build_node_structured_output(configuration, node)
            node_runtime_snapshots[node.node_id] = {
                "agent_id": profile.agent_id,
                "role": profile.role,
                "node_goal": node_goals[node.node_id],
                "system_prompt": build_node_system_prompt(
                    profile,
                    configuration,
                    node,
                    goal,
                    node_roles,
                ),
                "allowed_tools": allowed_tools,
                "structured_output": structured_output.to_document(),
                "model_config_id": profile.model_config_id or parent_agent_profile.model_config_id,
                "model_settings": dataclasses.asdict(model_settings),
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
                    "model_config_id": profile.model_config_id
                                       or parent_agent_profile.model_config_id,
                    "model_name": model_settings.model_name,
                    # 与 node_runtime_snapshots 用同一份工具名清单：预览展示的节点工具必须就是
                    # 确认后冻结执行的那一份，否则用户看到的与真正执行的不一致。
                    "tools": list(allowed_tools),
                    "max_steps": profile.max_steps,
                    "statuses": node.statuses,
                }
            )

        preview_fields = {
            "team_id": configuration.team_id,
            "name": configuration.name,
            "goal": goal,
            "node_goals": node_goals,
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
