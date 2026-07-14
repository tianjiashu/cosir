"""工具审批请求与决策记录。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ApprovalRequestRecord:
    """表示一次待用户处理的工具审批请求。

    参数:
        approval_id: 审批请求标识符。
        run_id: 所属运行标识符。
        step_id: 关联步骤标识符。
        tool_call_id: 关联工具调用标识符。
        tool_name: 工具名称。
        permission: 工具权限级别。
        risk_level: 风险等级。
        payload: 面向 UI 的审批详情。
        status: 审批状态。
        created_at: 创建时间。
        decided_at: 决策时间。

    返回:
        不可变审批请求记录。

    异常:
        无。

    副作用:
        无。
    """

    approval_id: str
    run_id: str
    step_id: Optional[str]
    tool_call_id: Optional[str]
    tool_name: str
    permission: str
    risk_level: str
    payload: Dict[str, Any]
    status: str
    created_at: datetime
    decided_at: Optional[datetime]

    def to_dict(self) -> Dict[str, Any]:
        """转换为 API 可序列化字典。

        参数:
            无。

        返回:
            审批请求字典。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "approval_id": self.approval_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "permission": self.permission,
            "risk_level": self.risk_level,
            "payload": self.payload,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
        }


@dataclass(frozen=True)
class ApprovalDecisionRecord:
    """表示一次审批决策。

    参数:
        decision_id: 审批决策标识符。
        approval_id: 关联审批请求标识符。
        decision: 决策值，例如 approved 或 denied。
        reason: 用户或系统给出的原因。
        decided_at: 决策时间。
        idempotency_key: 幂等键。

    返回:
        不可变审批决策记录。

    异常:
        无。

    副作用:
        无。
    """

    decision_id: str
    approval_id: str
    decision: str
    reason: Optional[str]
    decided_at: datetime
    idempotency_key: str

    def to_dict(self) -> Dict[str, Any]:
        """转换为 API 可序列化字典。

        参数:
            无。

        返回:
            审批决策字典。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "decision_id": self.decision_id,
            "approval_id": self.approval_id,
            "decision": self.decision,
            "reason": self.reason,
            "decided_at": self.decided_at.isoformat(),
            "idempotency_key": self.idempotency_key,
        }
