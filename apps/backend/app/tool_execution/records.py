"""工具调用与执行记录。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ToolCallRecord:
    """表示一次持久化工具调用。

    参数:
        tool_call_id: 工具调用标识符。
        run_id: 所属运行标识符。
        step_id: 关联步骤标识符。
        tool_name: 工具名称。
        arguments: 工具参数快照。
        permission: 权限级别。
        status: 调用状态。
        idempotency_key: 幂等键。
        created_at: 创建时间。
        updated_at: 更新时间。

    返回:
        不可变工具调用记录。

    异常:
        无。

    副作用:
        无。
    """

    tool_call_id: str
    run_id: str
    step_id: Optional[str]
    tool_name: str
    arguments: Dict[str, Any]
    permission: str
    status: str
    idempotency_key: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ToolExecutionRecord:
    """表示一次工具 handler 执行。

    参数:
        execution_id: 执行记录标识符。
        tool_call_id: 关联工具调用标识符。
        status: 执行状态。
        effect_status: 副作用状态。
        artifact_id: 可选 artifact 标识符。
        error: 可选错误信息。
        started_at: 开始时间。
        completed_at: 完成时间。

    返回:
        不可变工具执行记录。

    异常:
        无。

    副作用:
        无。
    """

    execution_id: str
    tool_call_id: str
    status: str
    effect_status: str
    artifact_id: Optional[str]
    error: Optional[str]
    started_at: datetime
    completed_at: Optional[datetime]


@dataclass(frozen=True)
class ToolPolicyDecision:
    """表示工具策略评估结果。

    参数:
        status: allow、approval_required 或 deny。
        risk_level: 风险等级。
        reason: 决策原因。
        permission: 工具权限级别。

    返回:
        不可变工具策略决策。

    异常:
        无。

    副作用:
        无。
    """

    status: str
    risk_level: str
    reason: str
    permission: str
