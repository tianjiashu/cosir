"""Delegation policy input and decision value objects."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DelegationPolicyContext:
    """Carry the primitive values required to evaluate a delegation request."""

    parent_agent_id: str
    child_agent_id: str
    requested_tools: tuple[str, ...]
    parent_allowed_tools: frozenset[str]
    child_allowed_tools: frozenset[str]
    system_allowed_tools: frozenset[str]
    depth: int
    max_depth: int
    running_children: int
    max_concurrency: int
    known_child_agent_ids: frozenset[str]


@dataclass(frozen=True)
class DelegationPolicyDecision:
    """Describe whether delegation is allowed and the resulting child tool set."""

    allowed: bool
    reason: str
    effective_tools: tuple[str, ...]
