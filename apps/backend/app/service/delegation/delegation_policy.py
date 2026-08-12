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
            context: 包含 child 工具权限、深度与已知 Agent 集合的不可变策略上下文。
                有效工具完全由 child 工具权限收敛决定，父 Agent 与系统级工具集合不再
                参与计算。并发额度（max_concurrency）的裁决已下沉到 storage 层原子
                acquire，本策略只负责深度、已知 Agent 与工具收敛三类校验。

        返回:
            包含是否允许、拒绝原因和有效工具列表的策略决策。有效工具为
            child_allowed_tools 剔除 ``delegate_task`` 后的集合（避免 child 递归委派），
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

        # 有效工具收敛，避免 child 递归委派
        effective_tools = context.child_allowed_tools - frozenset({"delegate_task"})
        if not effective_tools:
            return DelegationPolicyDecision(False, "no_effective_tools", ())
        return DelegationPolicyDecision(True, "", tuple(sorted(effective_tools)))
