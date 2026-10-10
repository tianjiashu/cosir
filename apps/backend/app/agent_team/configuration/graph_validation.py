"""Agent Team 图结构的共享校验逻辑。

本模块只依赖节点和转移对象的最小属性协议，因此既可以被面向 Agent 的提案参数模型
调用，也可以被最终持久化配置模型调用。字段格式由各自模型负责；本模块只负责节点
之间的结构关系、可达性和状态转移完整性，不负责读取文件、解析 Agent profile 或写库。

图以 ``start_node_id`` 作为唯一入口，以转移目标字面量 ``END`` 表示 Team 结束；节点不再
携带 ``node_type``。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from app.agent_team.configuration.team_transition_definition import END_TARGET_NODE_ID


class TeamGraphNode(Protocol):
    """图校验所需的节点最小属性协议。"""

    node_id: str
    statuses: Sequence[str]


class TeamGraphTransition(Protocol):
    """图校验所需的转移最小属性协议。"""

    from_node_id: str
    status: str
    target_node_id: str


class TeamGraphValidationError(ValueError):
    """Team 节点图不符合静态结构契约时抛出的异常。"""


def validate_team_graph(
    nodes: Sequence[TeamGraphNode],
    transitions: Sequence[TeamGraphTransition],
    start_node_id: str,
) -> None:
    """校验 Team 图的入口、转移关系和可达性。

    参数:
        nodes: 待校验的节点对象序列；对象只需实现 :class:`TeamGraphNode` 协议。
        transitions: 待校验的转移对象序列；对象只需实现 :class:`TeamGraphTransition`
            协议。
        start_node_id: 图的唯一入口节点标识，必须声明在 ``nodes`` 中。

    异常:
        TeamGraphValidationError: 节点重复、入口无效、``END`` 被用作节点、转移引用无效、
            存在孤立节点、死路节点、重复转移或状态缺少转移时抛出。

    副作用:
        不修改输入对象，不读取外部资源，也不产生持久化写入。
    """

    if not nodes:
        raise TeamGraphValidationError("Team must contain at least one node")

    node_ids = [node.node_id for node in nodes]
    if len(set(node_ids)) != len(node_ids):
        raise TeamGraphValidationError("node_id must be unique")
    node_map = {node.node_id: node for node in nodes}

    start_nodes = [node_id for node_id in node_ids if node_id == start_node_id]
    if len(start_nodes) != 1:
        raise TeamGraphValidationError(
            f"Team must have exactly one start node; '{start_node_id}' must identify it"
        )
    if END_TARGET_NODE_ID in node_map:
        raise TeamGraphValidationError(
            f"node_id '{END_TARGET_NODE_ID}' is reserved as the terminal target"
        )

    edge_keys: set[tuple[str, str]] = set()
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    adjacency[END_TARGET_NODE_ID] = set()
    reverse_adjacency: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    reverse_adjacency[END_TARGET_NODE_ID] = set()
    for transition in transitions:
        if transition.from_node_id == END_TARGET_NODE_ID:
            raise TeamGraphValidationError(
                f"'{END_TARGET_NODE_ID}' is only allowed as a transition target, "
                "not as from_node_id"
            )
        source = node_map.get(transition.from_node_id)
        if source is None:
            raise TeamGraphValidationError("transition references an unknown source node")
        if transition.status not in source.statuses:
            raise TeamGraphValidationError(
                "status "
                f"'{transition.status}' is not allowed by node '{transition.from_node_id}'"
            )

        edge_key = (transition.from_node_id, transition.status)
        if edge_key in edge_keys:
            raise TeamGraphValidationError(
                "multiple ambiguous transitions for node/status: "
                f"{transition.from_node_id}/{transition.status}"
            )
        edge_keys.add(edge_key)

        target = transition.target_node_id
        if target != END_TARGET_NODE_ID and target not in node_map:
            raise TeamGraphValidationError("transition references an unknown target node")
        adjacency[transition.from_node_id].add(target)
        reverse_adjacency[target].add(transition.from_node_id)

    for node in nodes:
        missing_statuses = [
            status for status in node.statuses if (node.node_id, status) not in edge_keys
        ]
        if missing_statuses:
            raise TeamGraphValidationError(
                "node "
                f"'{node.node_id}' has statuses without transitions: {', '.join(missing_statuses)}"
            )

    terminal_targets = {
        transition.target_node_id
        for transition in transitions
        if transition.target_node_id == END_TARGET_NODE_ID
    }
    if terminal_targets != {END_TARGET_NODE_ID}:
        raise TeamGraphValidationError(
            f"Team must use exactly one logical terminal target '{END_TARGET_NODE_ID}'"
        )

    reachable = _walk_graph(start_node_id, adjacency)
    unreachable = sorted(set(node_ids) - reachable)
    if unreachable:
        raise TeamGraphValidationError(f"unreachable nodes: {', '.join(unreachable)}")

    can_reach_end = _walk_graph(END_TARGET_NODE_ID, reverse_adjacency)
    dead_end_nodes = sorted(set(node_ids) - can_reach_end)
    if dead_end_nodes:
        raise TeamGraphValidationError(
            "nodes cannot reach the terminal END target: " + ", ".join(dead_end_nodes)
        )


def _walk_graph(start_node_id: str, adjacency: dict[str, set[str]]) -> set[str]:
    """从指定节点沿给定邻接表遍历并返回可达节点集合。"""

    reachable = {start_node_id}
    frontier = [start_node_id]
    while frontier:
        current = frontier.pop()
        for target in adjacency[current]:
            if target not in reachable:
                reachable.add(target)
                frontier.append(target)
    return reachable
