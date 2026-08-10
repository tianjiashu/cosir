"""Deterministic policy for first-version child delegation."""

from app.service.delegation.delegation_context import (
    DelegationPolicyContext,
    DelegationPolicyDecision,
)


class DelegationPolicy:
    """Evaluate delegation eligibility and effective tool permissions."""

    def resolve(self, context: DelegationPolicyContext) -> DelegationPolicyDecision:
        """Evaluate one delegation request against its policy context.

        参数:
            context: 包含 parent、child、深度、并发和工具权限的不可变策略上下文。

        返回:
            包含是否允许、拒绝原因和按请求顺序保留的有效工具列表的策略决策。

        异常:
            无。

        副作用:
            无。
        """

        if context.child_agent_id not in context.known_child_agent_ids:
            return DelegationPolicyDecision(False, "unknown_child_agent", ())
        if context.depth >= context.max_depth:
            return DelegationPolicyDecision(False, "delegation_depth_exceeded", ())
        if context.running_children >= context.max_concurrency:
            return DelegationPolicyDecision(False, "delegation_concurrency_exceeded", ())

        effective_tools: list[str] = []
        seen_tools: set[str] = set()
        for tool in context.requested_tools:
            if tool in seen_tools:
                continue
            seen_tools.add(tool)
            if (
                tool in context.parent_allowed_tools
                and tool in context.child_allowed_tools
                and tool in context.system_allowed_tools
            ):
                effective_tools.append(tool)
        if not effective_tools:
            return DelegationPolicyDecision(False, "no_effective_tools", ())
        return DelegationPolicyDecision(True, "", tuple(effective_tools))
