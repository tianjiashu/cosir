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
            context: 包含 parent、child、系统三方工具权限、深度、并发的不可变策略上下文。
                不再包含 requested_tools 维度——有效工具完全由三方权限交集决定。

        返回:
            包含是否允许、拒绝原因和有效工具列表的策略决策。有效工具为
            parent_allowed_tools ∩ child_allowed_tools ∩ system_allowed_tools 的交集，
            结果按字典序排序后转 tuple，保证跨运行确定性。

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

        effective_tools = (
            context.parent_allowed_tools
            & context.child_allowed_tools
            & context.system_allowed_tools
        )
        if not effective_tools:
            return DelegationPolicyDecision(False, "no_effective_tools", ())
        return DelegationPolicyDecision(True, "", tuple(sorted(effective_tools)))
