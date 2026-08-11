"""Delegation policy input and decision value objects."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DelegationPolicyContext:
    """Carry the primitive values required to evaluate a delegation request.

    参数:
        parent_agent_id: 发起委派的父 Agent 标识。
        child_agent_id: 目标 child Agent 标识。
        parent_allowed_tools: 父 Agent profile 声明的可用工具集合。
        child_allowed_tools: child Agent profile 声明的可用工具集合。
        system_allowed_tools: 系统策略允许 child 使用的工具集合（剔除 delegate_task）。
        depth: 当前委派链深度（父 turn 无 parent 时为 0）。
        running_children: 当前父 turn 下处于活跃状态的 child 数量。
        known_child_agent_ids: 注册表中已知 child Agent 标识集合。
        max_depth: 委派链最大允许深度，默认 1。
        max_concurrency: 同一父 turn 下最大并发 child 数，默认 1。

    返回:
        不可变的委派策略上下文值对象。

    异常:
        无。

    副作用:
        无。
    """

    parent_agent_id: str
    child_agent_id: str
    parent_allowed_tools: frozenset[str]
    child_allowed_tools: frozenset[str]
    system_allowed_tools: frozenset[str]
    depth: int
    running_children: int
    known_child_agent_ids: frozenset[str]
    max_depth: int = 1
    max_concurrency: int = 1


@dataclass(frozen=True)
class DelegationPolicyDecision:
    """Describe whether delegation is allowed and the resulting child tool set."""

    allowed: bool
    reason: str
    effective_tools: tuple[str, ...]
