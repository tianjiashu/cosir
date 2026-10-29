"""Agent Team 图结构的共享校验逻辑。

本模块只依赖节点和转移对象的最小属性协议，因此既可以被面向 Agent 的提案参数模型
调用，也可以被最终持久化配置模型调用。字段格式由各自模型负责；本模块只负责节点
之间的结构关系、可达性和状态转移完整性，不负责读取文件、解析 Agent profile 或写库。

图以 ``start_node_id`` 作为唯一入口，以转移目标字面量 ``END`` 表示 Team 结束；节点不再
携带 ``node_type``。

问题以带稳定代码与定位的 :class:`TeamGraphIssue` 表达：保存时一次性报告全部问题，
配置编辑器也经同一函数实时取得问题并标到节点与连线上。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.models.agent_team.configuration.team_transition_definition import END_TARGET_NODE_ID


class TeamGraphNode(Protocol):
    """图校验所需的节点最小属性协议。"""

    node_id: str
    statuses: Sequence[TeamGraphStatus]


class TeamGraphStatus(Protocol):
    """图校验所需的状态名称属性。"""

    name: str


class TeamGraphTransition(Protocol):
    """图校验所需的转移最小属性协议。"""

    from_node_id: str
    status: str
    target_node_id: str


class TeamGraphIssueCode(StrEnum):
    """图结构问题的稳定代码；面向用户的文案由 API 层按代码生成，不解析英文描述。"""

    NO_NODES = "no_nodes"
    DUPLICATE_NODE_ID = "duplicate_node_id"
    INVALID_START = "invalid_start"
    RESERVED_END_ID = "reserved_end_id"
    END_AS_SOURCE = "end_as_source"
    UNKNOWN_SOURCE = "unknown_source"
    UNDECLARED_STATUS = "undeclared_status"
    AMBIGUOUS_TRANSITION = "ambiguous_transition"
    UNKNOWN_TARGET = "unknown_target"
    STATUS_WITHOUT_TRANSITION = "status_without_transition"
    UNREACHABLE_NODE = "unreachable_node"
    DEAD_END_NODE = "dead_end_node"


@dataclass(frozen=True)
class TeamGraphIssue:
    """一条图结构问题及其定位。

    Attributes:
        code: 问题类别。
        detail: 英文描述，供日志与 Agent 工具反馈使用。
        node_id: 问题所在节点；Team 级问题为 ``None``。
        status: 涉及的业务状态（缺转移、未声明状态、重复转移）。
        transition_index: 涉及的转移在 ``transitions`` 中的下标。
    """

    code: TeamGraphIssueCode
    detail: str
    node_id: str | None = None
    status: str | None = None
    transition_index: int | None = None


class TeamGraphValidationError(ValueError):
    """Team 节点图不符合静态结构契约时抛出，携带全部问题而不只是第一条。"""

    def __init__(self, issues: Sequence[TeamGraphIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(issue.detail for issue in self.issues))


def validate_team_graph(
    nodes: Sequence[TeamGraphNode],
    transitions: Sequence[TeamGraphTransition],
    start_node_id: str,
) -> None:
    """校验 Team 图；存在任何问题时一次性抛出全部问题。

    异常:
        TeamGraphValidationError: ``collect_team_graph_issues`` 返回非空时抛出。
    """

    issues = collect_team_graph_issues(nodes, transitions, start_node_id)
    if issues:
        raise TeamGraphValidationError(issues)


def collect_team_graph_issues(
    nodes: Sequence[TeamGraphNode],
    transitions: Sequence[TeamGraphTransition],
    start_node_id: str,
) -> list[TeamGraphIssue]:
    """收集 Team 图的入口、转移关系与可达性问题。

    保存校验与编辑器实时提示共用本函数，规则只在这里维护一份。问题按节点 / 状态 /
    转移定位，便于前端标到对应位置。前置结构有问题时跳过依赖它的检查，避免噪声：
    节点 ID 重复时不判定可达性（无法确定连线归属）；入口无效时不判定从入口可达；
    引用无效的转移不计入连通关系。

    参数:
        nodes: 节点序列，只需实现 :class:`TeamGraphNode` 协议。
        transitions: 转移序列，只需实现 :class:`TeamGraphTransition` 协议。
        start_node_id: 唯一入口节点标识。

    返回:
        问题列表；为空表示图结构合法。不修改输入、不访问外部资源。
    """

    if not nodes:
        return [TeamGraphIssue(TeamGraphIssueCode.NO_NODES, "Team must contain at least one node")]

    issues: list[TeamGraphIssue] = []
    node_ids = [node.node_id for node in nodes]
    duplicated = sorted({node_id for node_id in node_ids if node_ids.count(node_id) > 1})
    issues.extend(
        TeamGraphIssue(
            TeamGraphIssueCode.DUPLICATE_NODE_ID,
            f"node_id must be unique: {node_id}",
            node_id=node_id,
        )
        for node_id in duplicated
    )
    if END_TARGET_NODE_ID in node_ids:
        issues.append(TeamGraphIssue(
            TeamGraphIssueCode.RESERVED_END_ID,
            f"node_id '{END_TARGET_NODE_ID}' is reserved as the terminal target",
            node_id=END_TARGET_NODE_ID,
        ))
    start_valid = node_ids.count(start_node_id) == 1
    if not start_valid:
        issues.append(TeamGraphIssue(
            TeamGraphIssueCode.INVALID_START,
            f"Team must have exactly one start node; '{start_node_id}' must identify it",
        ))

    node_map = {node.node_id: node for node in nodes if node.node_id not in duplicated}
    edge_keys: set[tuple[str, str]] = set()
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in node_map}
    adjacency[END_TARGET_NODE_ID] = set()
    reverse_adjacency: dict[str, set[str]] = {node_id: set() for node_id in node_map}
    reverse_adjacency[END_TARGET_NODE_ID] = set()
    for index, transition in enumerate(transitions):
        source_id = transition.from_node_id
        if source_id == END_TARGET_NODE_ID:
            issues.append(TeamGraphIssue(
                TeamGraphIssueCode.END_AS_SOURCE,
                f"'{END_TARGET_NODE_ID}' is only allowed as a transition target, "
                "not as from_node_id",
                transition_index=index,
            ))
            continue
        source = node_map.get(source_id)
        if source is None:
            if source_id not in duplicated:
                issues.append(TeamGraphIssue(
                    TeamGraphIssueCode.UNKNOWN_SOURCE,
                    f"transition references an unknown source node: {source_id}",
                    transition_index=index,
                ))
            continue
        if transition.status not in {_status_name(status) for status in source.statuses}:
            issues.append(TeamGraphIssue(
                TeamGraphIssueCode.UNDECLARED_STATUS,
                f"status '{transition.status}' is not allowed by node '{source_id}'",
                node_id=source_id, status=transition.status, transition_index=index,
            ))
            continue
        edge_key = (source_id, transition.status)
        if edge_key in edge_keys:
            issues.append(TeamGraphIssue(
                TeamGraphIssueCode.AMBIGUOUS_TRANSITION,
                f"multiple ambiguous transitions for node/status: {source_id}/{transition.status}",
                node_id=source_id, status=transition.status, transition_index=index,
            ))
            continue
        edge_keys.add(edge_key)
        target = transition.target_node_id
        if target != END_TARGET_NODE_ID and target not in node_map:
            if target not in duplicated:
                issues.append(TeamGraphIssue(
                    TeamGraphIssueCode.UNKNOWN_TARGET,
                    f"transition references an unknown target node: {target}",
                    node_id=source_id, status=transition.status, transition_index=index,
                ))
            continue
        adjacency[source_id].add(target)
        reverse_adjacency[target].add(source_id)

    for node_id, node in node_map.items():
        for status in node.statuses:
            status_name = _status_name(status)
            if (node_id, status_name) not in edge_keys:
                issues.append(TeamGraphIssue(
                    TeamGraphIssueCode.STATUS_WITHOUT_TRANSITION,
                    f"node '{node_id}' has statuses without transitions: {status_name}",
                    node_id=node_id, status=status_name,
                ))

    if duplicated:
        return issues
    if start_valid:
        reachable = _walk_graph(start_node_id, adjacency)
        issues.extend(
            TeamGraphIssue(
                TeamGraphIssueCode.UNREACHABLE_NODE,
                f"unreachable nodes: {node_id}",
                node_id=node_id,
            )
            for node_id in node_ids if node_id not in reachable
        )
    can_reach_end = _walk_graph(END_TARGET_NODE_ID, reverse_adjacency)
    issues.extend(
        TeamGraphIssue(
            TeamGraphIssueCode.DEAD_END_NODE,
            f"nodes cannot reach the terminal END target: {node_id}",
            node_id=node_id,
        )
        for node_id in node_ids if node_id not in can_reach_end
    )
    return issues


def _status_name(status: TeamGraphStatus) -> str:
    """读取参与路由的状态名称，颜色只属于展示配置。"""

    return status.name


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
