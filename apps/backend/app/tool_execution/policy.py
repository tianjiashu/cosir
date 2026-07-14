"""工具执行策略编排。"""

from typing import Iterable

from app.tool_execution.policy_provider import ToolPolicyContext, ToolPolicyProvider
from app.tool_execution.records import ToolPolicyDecision


class ToolExecutionPolicy:
    """聚合通用权限和具体工具族策略。"""

    def __init__(self, providers: Iterable[ToolPolicyProvider] = ()) -> None:
        """初始化策略编排器。

        参数:
            providers: 具体工具族策略 provider 列表。

        返回:
            无。

        异常:
            无。

        副作用:
            保存 provider 列表。
        """

        self._providers = tuple(providers)

    def decide(self, context: ToolPolicyContext) -> ToolPolicyDecision:
        """评估工具执行策略。

        参数:
            context: 工具策略上下文。

        返回:
            策略决策；没有 provider 支持时默认要求审批。

        异常:
            无。

        副作用:
            无。
        """

        for provider in self._providers:
            if provider.supports(context):
                return provider.decide(context)
        return ToolPolicyDecision(
            status="approval_required",
            risk_level="medium",
            reason="no policy provider matched tool",
            permission=context.permission,
        )
