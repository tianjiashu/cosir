"""ProposeAgentTeamConfigurationTool 的独立参数模型。

本模块只描述主 Agent 调用配置提案工具时可以提交的参数，不复用静态配置领域模型，
以便为工具 schema 提供面向 Agent 的精简英文描述。提案输入拥有独立的字段和图结构
校验；配置保存或加载时，``AgentTeamConfiguration`` 仍会按持久化契约再次校验。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

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
        description=(
            "Unique node identifier referenced by start_node_id and transitions. "
            "Allowed: letters, digits, '_' and '-'. Reserved: 'END'."
        ),
        examples=["develop"],
    )
    name: StrictStr = Field(
        min_length=1,
        max_length=256,
        description="Human-readable name for this node.",
        examples=["Development"],
    )
    agent_id: AgentTeamIdentifier = Field(
        description="ID of the registered child Agent profile that executes this node.",
        examples=["developer_agent"],
    )
    statuses: list[AgentTeamStatusName] = Field(
        min_length=1,
        max_length=32,
        description=(
            "Business statuses this node may report. Lowercase snake_case (e.g. pass, fail, "
            "needs_changes); not runtime lifecycle states. Each status needs exactly one "
            "outgoing transition."
        ),
        examples=[["pass", "fail"]],
    )


class ProposeAgentTeamTransitionArgs(BaseModel):
    """配置提案中的单条串行转移参数。"""

    model_config = ConfigDict(extra="forbid")

    from_node_id: AgentTeamIdentifier = Field(
        description=(
            "Source node that reports this status. Must be a declared node_id, never 'END'."
        ),
        examples=["review"],
    )
    status: AgentTeamStatusName = Field(
        description=(
            "Business status of from_node_id that triggers this transition; must be one of "
            "that node's statuses."
        ),
        examples=["needs_changes"],
    )
    target_node_id: AgentTeamIdentifier = Field(
        description=(
            "Next node_id, or the literal 'END' to finish the Team. 'END' is the only "
            "allowed terminal target."
        ),
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

        _validate_proposal_graph(self.nodes, self.transitions, self.start_node_id)
        return self

    team_id: StrictStr = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        description=(
            "Stable reusable Team identifier (letters, digits, '_', '-'). Used as the config "
            "file name and runtime reference."
        ),
        examples=["code_quality_team"],
    )
    name: StrictStr = Field(
        min_length=1,
        max_length=256,
        description="Human-readable name for the Team.",
        examples=["Code Quality Team"],
    )
    description: StrictStr = Field(
        min_length=1,
        max_length=2_000,
        description="Concise description of the Team's purpose and intended use.",
        examples=["develop, review, and test code changes."],
    )
    max_runs: int = Field(
        default=10,
        ge=1,
        description=(
            "Maximum number of node rounds the whole Team may run before it fails. "
            "Optional; defaults to 10."
        ),
        examples=[10],
    )
    start_node_id: StrictStr = Field(
        description=(
            "node_id of the single entry node where execution begins. Must be one of nodes "
            "and must not be 'END'."
        ),
        examples=["develop"],
    )
    nodes: list[ProposeAgentTeamNodeArgs] = Field(
        min_length=1,
        max_length=256,
        description=(
            "Pipeline node definitions. Exactly one entry node (start_node_id); do not "
            "declare an end node; route a transition to 'END' to finish. node_goals are "
            "supplied at run time, not here."
        ),
        examples=[
            [
                {
                    "node_id": "develop",
                    "name": "Development",
                    "agent_id": "developer_agent",
                    "statuses": ["done", "blocked"],
                }
            ]
        ],
    )
    transitions: list[ProposeAgentTeamTransitionArgs] = Field(
        max_length=2_000,
        description=(
            "Status transitions. Each (from_node_id, status) pair may appear at most once; "
            "target 'END' to finish the Team. Every declared status needs a transition, all "
            "nodes must be reachable from start_node_id, and the graph must be acyclic."
        ),
        examples=[
            [
                {
                    "from_node_id": "develop",
                    "status": "done",
                    "target_node_id": "review",
                },
                {
                    "from_node_id": "review",
                    "status": "blocked",
                    "target_node_id": "develop",
                },
                {
                    "from_node_id": "review",
                    "status": "done",
                    "target_node_id": "END",
                }
            ]
        ],
    )


END_TARGET_NODE_ID = "END"


def _validate_proposal_graph(
    nodes: Sequence[ProposeAgentTeamNodeArgs],
    transitions: Sequence[ProposeAgentTeamTransitionArgs],
    start_node_id: str,
) -> None:
    """校验提案输入的节点图。

    提案 schema 不携带 ``node_type``：唯一入口由 ``start_node_id`` 指定，Team 是否结束
    由转移目标字面量 ``END`` 表示（至少有一条转移可达 ``END``）。这里刻意保留提案模型
    自己的校验实现：工具输入的错误信息和演进节奏可以独立于持久化配置模型。该函数只读取
    已完成字段校验的提案对象，不访问文件、注册表或其他运行时资源。

    异常:
        ValueError: 节点、转移关系或图可达性不符合提案契约时抛出。
    """

    if not nodes:
        raise ValueError("Team must contain at least one node")

    node_ids = [node.node_id for node in nodes]
    if len(set(node_ids)) != len(node_ids):
        raise ValueError("node_id must be unique")
    node_map = {node.node_id: node for node in nodes}

    if start_node_id not in node_map:
        raise ValueError(f"start_node_id '{start_node_id}' is not a declared node")
    if END_TARGET_NODE_ID in node_map:
        raise ValueError(
            f"node_id '{END_TARGET_NODE_ID}' is reserved as the terminal target"
        )

    edge_keys: set[tuple[str, str]] = set()
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    adjacency[END_TARGET_NODE_ID] = set()
    reverse_adjacency: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    reverse_adjacency[END_TARGET_NODE_ID] = set()
    for transition in transitions:
        if transition.from_node_id == END_TARGET_NODE_ID:
            raise ValueError(
                f"'{END_TARGET_NODE_ID}' is only allowed as a transition target, "
                "not as from_node_id"
            )
        source = node_map.get(transition.from_node_id)
        if source is None:
            raise ValueError("transition references an unknown source node")
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

        target = transition.target_node_id
        if target != END_TARGET_NODE_ID and target not in node_map:
            raise ValueError("transition references an unknown target node")
        adjacency[transition.from_node_id].add(target)
        reverse_adjacency[target].add(transition.from_node_id)

    for node in nodes:
        missing_statuses = [
            status for status in node.statuses if (node.node_id, status) not in edge_keys
        ]
        if missing_statuses:
            raise ValueError(
                f"node '{node.node_id}' has statuses without transitions: "
                f"{', '.join(missing_statuses)}"
            )

    reachable = _walk_proposal_graph(start_node_id, adjacency)
    unreachable = sorted(set(node_ids) - reachable)
    if unreachable:
        raise ValueError(f"unreachable nodes: {', '.join(unreachable)}")

    can_reach_end = _walk_proposal_graph(END_TARGET_NODE_ID, reverse_adjacency)
    dead_end_nodes = sorted(set(node_ids) - can_reach_end)
    if dead_end_nodes:
        raise ValueError(
            "nodes cannot reach the terminal END target: " + ", ".join(dead_end_nodes)
        )


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
