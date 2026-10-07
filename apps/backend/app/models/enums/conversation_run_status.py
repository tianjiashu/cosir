"""Conversation Run execution status enumeration.

单一职责：作为一次用户与 Agent 轮次执行态的「单一事实来源」的稳定字符串枚举。
值即落库字符串，``str(status)`` 返回该稳定值，避免散落的字符串字面量产生拼写漂移。
"""

from enum import Enum


class ConversationRunStatus(str, Enum):
    """轮次执行状态。

    状态机：``pending -> running -> waiting_for_input -> running``，并由
    ``running`` 或 ``waiting_for_input`` 进入 ``completed``、``failed`` 或
    ``cancelled``。用户显式恢复仍允许 ``cancelled -> running``。
    """

    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

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
