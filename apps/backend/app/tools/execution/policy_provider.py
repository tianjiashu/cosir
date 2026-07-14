"""工具族策略 provider 协议。"""

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Protocol

from app.tools.execution.records import ToolPolicyDecision


@dataclass(frozen=True)
class ToolPolicyContext:
    """传给工具族策略 provider 的上下文。

    参数:
        tool_name: 工具名称。
        arguments: 工具参数。
        permission: 工具声明权限。
        run_id: 所属运行标识符。

    返回:
        不可变策略上下文。

    异常:
        无。

    副作用:
        无。
    """

    tool_name: str
    arguments: Dict[str, Any]
    permission: str
    run_id: str


class ToolPolicyProvider(Protocol):
    """具体工具族的策略评估协议。"""

    def supports(self, context: ToolPolicyContext) -> bool:
        """返回 provider 是否支持该工具上下文。

        参数:
            context: 工具策略上下文。

        返回:
            支持时返回 True。

        异常:
            无。

        副作用:
            无。
        """

    def decide(self, context: ToolPolicyContext) -> ToolPolicyDecision:
        """返回工具策略决策。

        参数:
            context: 工具策略上下文。

        返回:
            工具策略决策。

        异常:
            ValueError: 如果上下文不被 provider 支持。

        副作用:
            无。
        """


class PermissionPolicyProvider:
    """按声明权限提供默认工具策略。"""

    def __init__(
        self,
        auto_approved_permissions: Iterable[str] = (),
        approval_required_permissions: Iterable[str] = (),
    ) -> None:
        """初始化权限策略配置。

        参数:
            auto_approved_permissions: 可以直接执行的权限集合。
            approval_required_permissions: 必须先经人工审批的权限集合。

        返回:
            无。

        异常:
            无。

        副作用:
            保存不可变权限集合。
        """

        self._auto_approved_permissions = frozenset(auto_approved_permissions)
        self._approval_required_permissions = frozenset(approval_required_permissions)

    def supports(self, context: ToolPolicyContext) -> bool:
        """返回该通用权限策略是否适用于工具。

        参数:
            context: 正在评估的工具策略上下文。

        返回:
            始终为 True，作为策略链的默认 provider。

        异常:
            无。

        副作用:
            无。
        """

        return True

    def decide(self, context: ToolPolicyContext) -> ToolPolicyDecision:
        """根据工具权限返回允许、审批或拒绝决策。

        参数:
            context: 正在评估的工具策略上下文。

        返回:
            不可变策略决策。

        异常:
            无。

        副作用:
            无。
        """

        if context.permission in self._auto_approved_permissions:
            return ToolPolicyDecision("allow", "low", "permission auto approved", context.permission)
        if context.permission in self._approval_required_permissions:
            return ToolPolicyDecision(
                "approval_required",
                "medium",
                "permission requires approval",
                context.permission,
            )
        return ToolPolicyDecision("deny", "high", "permission denied", context.permission)
