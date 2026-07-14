"""工具调用执行生命周期状态机。"""

from app.tools.execution.records import ToolPolicyDecision


class ToolExecutionLifecycle:
    """校验并映射工具调用的持久化状态。"""

    _TRANSITIONS = {
        "requested": {"approved", "waiting_approval", "cancelled", "failed"},
        "waiting_approval": {"approved", "cancelled"},
        "approved": {"running", "cancelled"},
        "running": {"completed", "failed", "cancelled"},
        "completed": set(),
        "failed": set(),
        "cancelled": set(),
    }

    def status_for_policy(self, decision: ToolPolicyDecision) -> str:
        """将策略决策转换为工具执行生命周期状态。

        参数:
            decision: 工具策略评估结果。

        返回:
            可持久化的生命周期状态。

        异常:
            ValueError: 当策略状态未知时抛出。

        副作用:
            无。
        """

        statuses = {"allow": "approved", "approval_required": "waiting_approval", "deny": "cancelled"}
        try:
            return statuses[decision.status]
        except KeyError as exc:
            raise ValueError(f"unsupported policy status: {decision.status}") from exc

    def ensure_transition(self, current: str, target: str) -> None:
        """校验一次工具调用状态迁移是否合法。

        参数:
            current: 当前持久化状态。
            target: 目标持久化状态。

        返回:
            无。

        异常:
            ValueError: 当迁移非法时抛出。

        副作用:
            无。
        """

        if current == target:
            return
        if target not in self._TRANSITIONS.get(current, set()):
            raise ValueError(f"invalid tool lifecycle transition: {current} -> {target}")
