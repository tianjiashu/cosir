"""Durable Run State 的持久化值对象。"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class RunRecord:
    """表示一次可恢复运行的持久化状态。

    参数:
        run_id: 运行记录标识符。
        task_id: 关联的任务标识符。
        thread_id: LangGraph checkpointer 使用的线程标识符。
        status: 当前运行状态。
        wait_reason: 等待状态的原因，例如 approval 或 human_input。
        active_step_id: 当前活跃步骤标识符。
        active_wait_id: 当前等待点标识符。
        last_checkpoint_id: 最近稳定 checkpoint 标识符。
        interruption_reason: 中断或需要人工复核的原因。
        created_at: 记录创建时间。
        updated_at: 记录更新时间。

    返回:
        不可变的运行状态记录。

    异常:
        无。

    副作用:
        无。
    """

    run_id: str
    task_id: str
    thread_id: str
    status: str
    wait_reason: Optional[str]
    active_step_id: Optional[str]
    active_wait_id: Optional[str]
    last_checkpoint_id: Optional[str]
    interruption_reason: Optional[str]
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> Dict[str, Any]:
        """转换为 API 可序列化字典。

        参数:
            无。

        返回:
            包含运行状态字段的字典。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "thread_id": self.thread_id,
            "status": self.status,
            "wait_reason": self.wait_reason,
            "active_step_id": self.active_step_id,
            "active_wait_id": self.active_wait_id,
            "last_checkpoint_id": self.last_checkpoint_id,
            "interruption_reason": self.interruption_reason,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True)
class ResumeCommandRecord:
    """表示一次恢复命令的持久化记录。

    参数:
        command_id: 恢复命令标识符。
        run_id: 被恢复的运行标识符。
        action: 恢复动作，例如 approve_tool、deny_tool 或 cancel_run。
        payload: 恢复动作载荷。
        idempotency_key: 用于避免重复恢复副作用的幂等键。
        status: 命令状态。
        created_at: 命令创建时间。
        applied_at: 命令应用时间。

    返回:
        不可变的恢复命令记录。

    异常:
        无。

    副作用:
        无。
    """

    command_id: str
    run_id: str
    action: str
    payload: Dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    status: str = "pending"
    created_at: Optional[datetime] = None
    applied_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        """转换为 API 可序列化字典。

        参数:
            无。

        返回:
            包含恢复命令字段的字典。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "command_id": self.command_id,
            "run_id": self.run_id,
            "action": self.action,
            "payload": self.payload,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "applied_at": self.applied_at.isoformat() if self.applied_at else None,
        }
