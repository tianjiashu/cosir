"""工具权限审批策略原语。"""

from dataclasses import dataclass
from typing import Iterable, Set

from app.tools.types import ToolDefinition


@dataclass(frozen=True)
class ToolApprovalDecision:
    """表示对一个工具执行请求的审批决定。

    参数:
        status: 决定状态，例如 ``approved``、``approval_required`` 或 ``denied``。
        reason: 用于诊断和 UI 事件的人类可读原因。

    返回:
        不可变的工具审批决定。

    异常:
        无。

    副作用:
        无。
    """

    status: str
    reason: str


class ToolApprovalPolicy:
    """将工具权限分类为已批准、需审批或已拒绝。"""

    def __init__(
        self,
        auto_approved_permissions: Iterable[str],
        approval_required_permissions: Iterable[str] = (),
    ) -> None:
        """初始化权限审批策略。

        参数:
            auto_approved_permissions: 可以立即执行的权限级别。
            approval_required_permissions: 对模型可见、但执行前需要用户审批的
                权限级别。

        返回:
            无。

        异常:
            无。

        副作用:
            在内存中保存权限集合。
        """

        self._auto_approved_permissions: Set[str] = set(auto_approved_permissions)
        self._approval_required_permissions: Set[str] = set(approval_required_permissions)

    def decide(self, tool: ToolDefinition) -> ToolApprovalDecision:
        """返回对一个工具的审批决定。

        参数:
            tool: 被请求的工具定义。

        返回:
            描述工具能否执行、需要审批还是被拒绝的决定。

        异常:
            无。

        副作用:
            无。
        """

        if tool.permission in self._auto_approved_permissions:
            return ToolApprovalDecision(status="approved", reason="auto approved")
        if tool.permission in self._approval_required_permissions:
            return ToolApprovalDecision(status="approval_required", reason="user approval required")
        return ToolApprovalDecision(status="denied", reason=f"permission denied: {tool.permission}")

    def is_visible_to_model(self, tool: ToolDefinition) -> bool:
        """返回工具是否应被暴露给模型。

        参数:
            tool: 待检查的工具定义。

        返回:
            当权限为自动批准或可由用户批准时为 True。

        异常:
            无。

        副作用:
            无。
        """

        return (
            tool.permission in self._auto_approved_permissions
            or tool.permission in self._approval_required_permissions
        )
