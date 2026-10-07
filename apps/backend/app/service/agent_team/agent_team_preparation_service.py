"""Agent Team 执行准备服务。

本模块把已解析的 Team 配置转换成一次运行准备结果：节点 Profile、工具定义、模型设置、
供展示层投影的预览输入和预览指纹均在同一次解析中生成。它不保存配置文件，不创建 TeamRun，
也不推进节点执行；这些职责分别由配置服务和 Team coordinator 负责。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from hashlib import sha256
from typing import Any

from app.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.agent_team.configuration.team_node_definition import TeamNodeDefinition
from app.config.configuration import get_agent_registry, get_tool_system
from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.model_settings import ModelSettings, ModelSettingsError
from app.core.agents.structured_output_spec import StructuredOutputSpec
from app.core.tools.schemas import ToolDefinition
from app.core.tools.schemas.tool_names import *

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
    ``node_runtime_snapshots`` 面向确认后的节点执行，``preview_fingerprint`` 是本次解析
    结果的稳定指纹。三者来自同一次解析，但最终确认时必须基于用户提交的配置重新生成，
    不能把展示数据直接当作执行事实。
    """

    preview_fields: dict[str, Any]
    node_runtime_snapshots: dict[str, dict[str, Any]]
    preview_fingerprint: str


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
        ValueError: 节点引用的 Agent 不存在或不是 CHILD 类型。
    """

    node = next((item for item in configuration.nodes if item.node_id == node_id), None)
    if node is None:
        raise KeyError(node_id)
    profile = get_agent_registry().resolve(workspace_root, node.agent_id)
    if profile is None or profile.agent_type is not AgentProfileType.CHILD:
        raise ValueError(f"Team 节点引用的 child Agent 不可用: {node.agent_id}")
    return node, profile


def materialize_node_tools(agent_id: str, workspace_root: str) -> list[ToolDefinition]:
    """根据节点 Agent Profile 固化工具定义。

    Team 节点不能递归创建 Team、子 Agent 或配置提案；可固化工具来自节点 Profile 允许的工具
    集，再剔除 Team 节点禁用的工具。返回的工具定义属于本次准备结果，确认后不会再次从可变
    注册表解析。

    异常:
        ValueError: Agent Profile 不存在。
    """

    tool_system = get_tool_system()
    profile = get_agent_registry().resolve(workspace_root, agent_id)
    if profile is None:
        raise ValueError(f"Team 节点 Agent 不可用: {agent_id}")
    selected = [
        tool
        for tool in profile.select_tools(tool_system.executor.list_tools())
        if tool.name not in TEAM_NODE_DISALLOWED_TOOLS
    ]
    return selected


def resolve_effective_model_settings(
    profile: AgentProfile,
    fallback: ModelSettings | None,
) -> ModelSettings:
    """解析节点确认时实际使用的完整模型设置。

    子 Agent Profile 可能只有用户偏好而没有独立运行配置；此时沿用主 Agent 本次 Run 已
    物化的模型连接配置，并应用节点偏好覆盖。返回值会进入本次 Team 的冻结运行快照。

    异常:
        ValueError: Profile 和 fallback 都不能提供完整模型运行配置。
    """

    try:
        return profile.model_settings.require_runtime_config()
    except ModelSettingsError:
        if fallback is None:
            raise ValueError(f"节点 Agent {profile.agent_id} 缺少可用的模型运行配置") from None
        try:
            effective = fallback.with_preference_defaults(profile.model_settings)
            return effective.require_runtime_config()
        except ModelSettingsError as exc:
            raise ValueError(f"节点 Agent {profile.agent_id} 缺少可用的模型运行配置") from exc


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
        ValueError: 节点状态没有对应转移，且节点不是终点时抛出。

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
            if node.node_type != "end":
                raise ValueError(
                    f"Team 节点状态缺少转移目标: {node.node_id}/{status}"
                )
            destination = "结束 Team"
        else:
            destination = f"{node_names[target_node_id]}（{target_node_id}）"
        route_descriptions.append(f"{status} → {destination}")

    schema = {
        "type": "object",
        "description": "提交当前 Team 节点的处理状态和供后续工作使用的结果。",
        "properties": {
            "status": {
                "type": "string",
                "enum": node.statuses,
                "description": (
                    "选择当前节点的处理状态；该值决定 Team 工作流流向，不是 Run 生命周期状态。"
                    "可选状态及去向：" + "；".join(route_descriptions)
                ),
            },
            "output": {
                "type": "string",
                "description": "简洁说明本节点结论及可供后续节点使用的必要信息。",
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
) -> str:
    """在节点 Profile 提示词后追加本次 Team 身份和流转协议。

    参数:
        profile: 已解析的节点 Agent Profile；本函数不修改该对象。
        configuration: 本次运行冻结的 Team 工作流配置。
        node: 当前节点定义。

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
        target = configuration.node(transition.target_node_id)
        route_lines.append(f"- {transition.status}：交给 {target.name}（{target.node_id}）")
    if node.node_type == "end":
        route_lines.append("- 本节点完成后结束 Team")

    workflow_nodes = [
        f"- {item.name}（{item.node_id}，{item.node_type}）"
        for item in configuration.nodes
    ]
    workflow_transitions = [
        f"- {configuration.node(item.from_node_id).name} --{item.status}--> "
        f"{configuration.node(item.target_node_id).name}"
        for item in configuration.transitions
    ]
    team_context = "\n".join(
        (
            "## Agent Team 执行协议",
            f"你正在 Team「{configuration.name}」（{configuration.team_id}）中执行。",
            f"当前节点：{node.name}（{node.node_id}，{node.node_type}）。",
            "Team 节点：\n" + "\n".join(workflow_nodes),
            "Team 状态流转：\n"
            + ("\n".join(workflow_transitions) or "- 无显式转移"),
            "当前节点状态与流向：\n" + ("\n".join(route_lines) or "- 完成后结束 Team"),
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
        instructions: dict[str, str],
        workspace_root: str,
        parent_task_id: int,
        parent_run_id: int,
        workspace_id: int,
        fallback_model_settings: ModelSettings | None = None,
        fallback_model_config_id: int | None = None,
    ) -> AgentTeamPreparationResult:
        """一次性解析 Team 节点并生成展示预览、运行快照和指纹。

        参数:
            configuration: 已通过 Team 配置领域校验的配置对象。
            goal: 本次 Team 执行目标，不能为空白。
            instructions: 按节点标识提供的附加指令，未知节点会被拒绝。
            workspace_root: 节点 Agent 和工具解析使用的 workspace 根路径。
            parent_task_id: 主 Agent Task 标识，写入展示预览。
            parent_run_id: 主 Agent Run 标识，写入展示预览。
            workspace_id: workspace 标识，写入展示预览。
            fallback_model_settings: 节点缺少独立连接配置时使用的主 Agent 模型设置。
            fallback_model_config_id: 节点缺少独立配置 ID 时使用的主 Agent 配置 ID。

        返回:
        同一次运行时解析产生的预览文档、节点运行快照和指纹。预览文档只供工具结果展示，
        不由本服务持久化。

        异常:
            ValueError: 目标、节点指令或节点运行时依赖无效。
    """

        goal = goal.strip()
        if not goal:
            raise ValueError("Team goal must not be blank")
        instructions = {
            key: value.strip() for key, value in instructions.items() if value.strip()
        }
        unknown_instructions = set(instructions) - {
            node.node_id for node in configuration.nodes
        }
        if unknown_instructions:
            raise ValueError(
                "instructions references unknown nodes: "
                + ", ".join(sorted(unknown_instructions))
            )

        setting_names = {field.name for field in fields(ModelSettings)}
        resolved_nodes: list[dict[str, Any]] = []
        node_runtime_snapshots: dict[str, dict[str, Any]] = {}
        for node in configuration.nodes:
            _, profile = resolve_node_profile(configuration, workspace_root, node.node_id)
            tools = materialize_node_tools(profile.agent_id, workspace_root)
            model_settings = resolve_effective_model_settings(profile, fallback_model_settings)
            structured_output = build_node_structured_output(configuration, node)
            node_runtime_snapshots[node.node_id] = {
                "agent_id": profile.agent_id,
                "role": profile.role,
                "system_prompt": build_node_system_prompt(profile, configuration, node),
                "instruction": instructions.get(node.node_id, ""),
                "allowed_tools": [tool.name for tool in tools],
                "max_steps": profile.max_steps,
                "structured_output": structured_output.to_document(),
                "model_config_id": profile.model_config_id or fallback_model_config_id,
                "model_settings": {
                    name: getattr(model_settings, name)
                    for name in setting_names
                    if getattr(model_settings, name) is not None
                },
            }
            resolved_nodes.append(
                {
                    "node_id": node.node_id,
                    "name": node.name,
                    "agent_id": node.agent_id,
                    "node_type": node.node_type,
                    "role": profile.role,
                    "instruction": instructions.get(node.node_id, ""),
                    "model_config_id": profile.model_config_id
                    or fallback_model_config_id,
                    "model_name": model_settings.model_name,
                    "tools": [tool.name for tool in tools],
                    "max_steps": profile.max_steps,
                    "statuses": node.statuses,
                }
            )

        preview_fields = {
            "team_id": configuration.team_id,
            "name": configuration.name,
            "goal": goal,
            "instructions": instructions,
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
        preview_fingerprint = sha256(
            json.dumps(
                {
                    "team_id": configuration.team_id,
                    "goal": goal,
                    "instructions": instructions,
                    "configuration": configuration.model_dump(mode="json"),
                    "node_runtime_snapshots": node_runtime_snapshots,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return AgentTeamPreparationResult(
            preview_fields=preview_fields,
            node_runtime_snapshots=node_runtime_snapshots,
            preview_fingerprint=preview_fingerprint,
        )
