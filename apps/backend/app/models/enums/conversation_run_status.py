"""Conversation Run execution status enumeration.

单一职责：作为一次用户与 Agent 轮次执行态的「单一事实来源」的稳定字符串枚举。
值即落库字符串，``str(status)`` 返回该稳定值，避免散落的字符串字面量产生拼写漂移。
"""

from enum import Enum


class ConversationRunStatus(str, Enum):
    """轮次执行状态。

    状态机：``pending -> running -> completed|failed|cancelled|interrupted``。
    用户主动取消的 ``cancelled`` run（``end_reason=user_cancelled``）允许再次迁移到
    ``running``；其他取消仍为终态。
    ``waiting_for_approval`` 本轮不纳入（审批异步持久化暂缓，YAGNI）。
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    def __str__(self) -> str:
        """返回状态稳定字符串值。

        参数:
            无。

        返回:
            可用于存储、日志与状态比较的字面量。

        异常:
            无。

        副作用:
            无。
        """

        return self.value
