"""Delegation policy input and decision value objects."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DelegationPolicyContext:
    """Carry the primitive values required to evaluate a delegation request.

    参数:
        parent_agent_id: 发起委派的父 Agent 标识。
        child_agent_id: 目标 child Agent 标识。
        child_allowed_tools: child Agent profile 声明的可用工具集合。
            ``delegate_task`` 的剔除由 ``DelegationPolicy.resolve`` 在处理过程中完成，
            调用方无需预先过滤，从而避免 child 获得委派能力而产生递归委派。
        depth: 当前委派链深度（父 turn 无 parent 时为 0）。
        known_child_agent_ids: 注册表中已知 child Agent 标识集合。
        max_depth: 委派链最大允许深度，默认 1。并发额度（``max_concurrency``）
            的实际裁决已下沉到 storage 层的原子 acquire（``try_create_pending``），
            由 executor 从 ``Settings.DELEGATION_MAX_CONCURRENCY`` 读取后传入，
            策略层只负责深度、已知 Agent 与工具收敛，不再做并发计数校验。

    返回:
        不可变的委派策略上下文值对象。

    异常:
        无。

    副作用:
        无。
    """

    parent_agent_id: str
    child_agent_id: str
    child_allowed_tools: frozenset[str]
    depth: int
    known_child_agent_ids: frozenset[str]
    max_depth: int = 1


@dataclass(frozen=True)
class DelegationPolicyDecision:
    """Describe whether delegation is allowed and the resulting child tool set."""

    allowed: bool
    reason: str
    effective_tools: tuple[str, ...]
