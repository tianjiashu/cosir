"""ProposeAgentTeamConfigurationTool 的独立参数模型。

本模块只描述主 Agent 调用配置提案工具时可以提交的参数，不复用静态配置领域模型，
以便为工具 schema 提供面向 Agent 的精简英文描述。提案输入拥有独立的字段和图结构
校验；配置保存或加载时，``AgentTeamConfiguration`` 仍会按持久化契约再次校验。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, StringConstraints, model_validator

AgentTeamIdentifier = Annotated[
    StrictStr,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
]
AgentTeamStatusName = Annotated[
    StrictStr,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
]


class ProposeAgentTeamNodeArgs(BaseModel):
    """配置提案中的节点参数。"""

    model_config = ConfigDict(extra="forbid")

    node_id: AgentTeamIdentifier = Field(
        description="Stable unique node ID used by transitions and runtime node goals.",
        examples=["develop"],
    )
    name: StrictStr = Field(
        min_length=1,
        max_length=256,
        description="Human-readable name for this pipeline node.",
        examples=["Development"],
    )
    agent_id: AgentTeamIdentifier = Field(
        description="ID of the child Agent profile that executes this node.",
        examples=["developer_agent"],
    )
    node_type: Literal["start", "middle", "end"] = Field(
        description=(
            "Pipeline position: start is the unique entry node, middle is a regular node, "
            "and end completes the Team after submitting its result."
        ),
        examples=["start"],
    )
    statuses: list[AgentTeamStatusName] = Field(
        min_length=1,
        max_length=32,
        description=(
            "Business statuses this node may submit when reporting its result. "
            "Use lowercase snake_case names, such as pass, fail, or needs_changes. "
            "Do not use runtime lifecycle states unless they are intentional business statuses."
        ),
        examples=[["pass", "fail"]],
    )


class ProposeAgentTeamTransitionArgs(BaseModel):
    """配置提案中的单条串行转移参数。"""

    model_config = ConfigDict(extra="forbid")

    from_node_id: AgentTeamIdentifier = Field(
        description="Source node ID whose submitted status triggers this transition.",
        examples=["review"],
    )
    status: AgentTeamStatusName = Field(
        description="Lowercase snake_case business status that triggers this transition.",
        examples=["needs_changes"],
    )
    target_node_id: AgentTeamIdentifier = Field(
        description="Next node ID. The Team ends when execution reaches a node of type end.",
        examples=["develop"],
    )


class ProposeAgentTeamConfigurationArgs(BaseModel):
    """主 Agent 生成一份待用户确认的 Team 配置候选。

    节点 ``node_goals`` 不属于这里的静态配置；主 Agent 在调用 ``agent_team`` 启动具体
    Team 时通过运行时参数提供。该模型只负责工具输入 schema，最终领域校验由
    ``AgentTeamConfiguration`` 在保存或加载配置时独立执行。
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_graph(self) -> ProposeAgentTeamConfigurationArgs:
        """按工具提案契约校验节点图，尽早反馈孤立节点和无效转移。

        返回:
            当前已通过参数和图结构校验的提案对象。

        异常:
            ValueError: 提案图结构不符合工具契约时抛出，由 Pydantic 转换为
                ``ValidationError``。
        """

        _validate_proposal_graph(self.nodes, self.transitions)
        return self

    @property
    def start_node_id(self) -> str:
        """返回提案中唯一的起始节点标识。

        图校验已经保证恰好存在一个 ``start`` 节点，因此这里仅负责从已校验的
        提案中读取入口，不再次执行完整图校验。
        """

        return next(node.node_id for node in self.nodes if node.node_type == "start")

    team_id: StrictStr = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        description=(
            "Stable reusable Team ID. Use only letters, numbers, underscores, and hyphens."
        ),
        examples=["code_quality_team"],
    )
    name: StrictStr = Field(
        min_length=1,
        max_length=256,
        description="Human-readable name for the reusable Team.",
        examples=["Code Quality Pipeline"],
    )
    description: StrictStr = Field(
        min_length=1,
        max_length=2_000,
        description="Concise description of the Team's purpose and intended use.",
        examples=["Implement, review, and test code changes."],
    )
    nodes: list[ProposeAgentTeamNodeArgs] = Field(
        min_length=1,
        max_length=256,
        description=(
            "Pipeline node definitions. Each node references a child Agent profile and "
            "declares its node type and allowed lowercase snake_case business statuses; "
            "node_goals values are supplied at run time."
        ),
        examples=[
            [
                {
                    "node_id": "develop",
                    "name": "Development",
                    "agent_id": "developer_agent",
                    "node_type": "start",
                    "statuses": ["done", "blocked"],
                }
            ]
        ],
    )
    transitions: list[ProposeAgentTeamTransitionArgs] = Field(
        max_length=2_000,
        description=(
            "Serial status transitions. Each source node and status may have at most one "
            "transition; route to an end node to finish the Team."
        ),
        examples=[
            [
                {
                    "from_node_id": "develop",
                    "status": "done",
                    "target_node_id": "review",
                }
            ]
        ],
    )


def _validate_proposal_graph(
    nodes: Sequence[ProposeAgentTeamNodeArgs],
    transitions: Sequence[ProposeAgentTeamTransitionArgs],
) -> None:
    """校验提案输入的节点图。

    这里刻意保留提案模型自己的校验实现：工具输入的错误信息和演进节奏可以独立于
    持久化配置模型。该函数只读取已完成字段校验的提案对象，不访问文件、注册表或
    其他运行时资源。

    异常:
        ValueError: 节点、转移关系或图可达性不符合提案契约时抛出。
    """

    if not nodes:
        raise ValueError("Team must contain at least one node")

    node_ids = [node.node_id for node in nodes]
    if len(set(node_ids)) != len(node_ids):
        raise ValueError("node_id must be unique")
    node_map = {node.node_id: node for node in nodes}

    start_nodes = [node for node in nodes if node.node_type == "start"]
    end_nodes = [node for node in nodes if node.node_type == "end"]
    if len(start_nodes) != 1:
        raise ValueError("Team must contain exactly one start node")
    if len(end_nodes) != 1:
        raise ValueError("Team must contain exactly one end node")

    edge_keys: set[tuple[str, str]] = set()
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    reverse_adjacency: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for transition in transitions:
        source = node_map.get(transition.from_node_id)
        if source is None:
            raise ValueError("transition references an unknown source node")
        if source.node_type == "end":
            raise ValueError("end nodes cannot have outgoing transitions")
        if transition.status not in source.statuses:
            raise ValueError(
                f"status '{transition.status}' is not allowed by node "
                f"'{transition.from_node_id}'"
            )

        edge_key = (transition.from_node_id, transition.status)
        if edge_key in edge_keys:
            raise ValueError(
                "multiple ambiguous transitions for node/status: "
                f"{transition.from_node_id}/{transition.status}"
            )
        edge_keys.add(edge_key)

        if transition.target_node_id not in node_map:
            raise ValueError("transition references an unknown target node")
        adjacency[transition.from_node_id].add(transition.target_node_id)
        reverse_adjacency[transition.target_node_id].add(transition.from_node_id)

    for node in nodes:
        if node.node_type == "end":
            continue
        missing_statuses = [
            status for status in node.statuses if (node.node_id, status) not in edge_keys
        ]
        if missing_statuses:
            raise ValueError(
                f"node '{node.node_id}' has statuses without transitions: "
                f"{', '.join(missing_statuses)}"
            )

    start_node_id = start_nodes[0].node_id
    reachable = _walk_proposal_graph(start_node_id, adjacency)
    unreachable = sorted(set(node_ids) - reachable)
    if unreachable:
        raise ValueError(f"unreachable nodes: {', '.join(unreachable)}")

    end_node_id = end_nodes[0].node_id
    can_reach_end = _walk_proposal_graph(end_node_id, reverse_adjacency)
    dead_end_nodes = sorted(set(node_ids) - can_reach_end)
    if dead_end_nodes:
        raise ValueError("nodes cannot reach the end node: " + ", ".join(dead_end_nodes))


def _walk_proposal_graph(
    start_node_id: str,
    adjacency: dict[str, set[str]],
) -> set[str]:
    """沿提案图遍历并返回可达节点集合。"""

    reachable = {start_node_id}
    frontier = [start_node_id]
    while frontier:
        current = frontier.pop()
        for target in adjacency[current]:
            if target not in reachable:
                reachable.add(target)
                frontier.append(target)
    return reachable
