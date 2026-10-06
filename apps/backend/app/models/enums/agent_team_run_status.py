"""Agent Team 执行意图与运行生命周期状态。"""

from enum import Enum


class AgentTeamRunStatus(str, Enum):
    """描述 Agent Team 从创建到终态的持久化生命周期。

    ``PENDING`` 专门表示等待用户确认；确认入口会把它原子迁移为 ``RUNNING``，因此不
    需要额外的确认标记或第二套状态机。
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    def __str__(self) -> str:
        """返回数据库和接口使用的稳定状态值。"""

        return self.value
