"""Delegation policy decision value object."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DelegationPolicyDecision:
    """描述一次委派请求是否被允许以及最终生效的 child 工具集合。

    参数:
        allowed: 策略是否放行本次委派。
        reason: 拒绝原因短码（如 ``unknown_child_agent`` / ``delegation_depth_exceeded`` /
            ``no_effective_tools``）；放行时为空字符串。
        effective_tools: 生效的 child 工具集合（已剔除 ``delegate_task`` 并字典序排序），
            tuple 化保证跨运行确定性；拒绝时为空 tuple。

    返回:
        不可变的委派策略决策值对象。

    异常:
        无。

    副作用:
        无。
    """

    allowed: bool
    reason: str
    effective_tools: tuple[str, ...]
