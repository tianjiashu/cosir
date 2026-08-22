"""任务持久化状态值对象。

单一职责：承载一个 Agent 任务的不可变业务数据并提供序列化（to_dict）。
不负责数据库操作（由 ``storage/crud/task_crud`` 负责）。
"""

from dataclasses import dataclass
from datetime import datetime

from app.storage.model.task_model import TaskModel
from app.utils.datetime_utils import from_text, to_text


@dataclass
class TaskRecord:
    """表示一个 Agent 任务的持久化状态。"""

    task_id: str
    workspace_id: str
    agent_id: str
    title: str
    status: str
    created_at: datetime
    updated_at: datetime
    execution_status: str | None = None
    task_type: str = "user"
    parent_task_id: str | None = None
    parent_turn_id: str | None = None
    delegation_id: str | None = None
    context_usage_used: int | None = None

    @property
    def is_child(self) -> bool:
        """判断该任务是否为委派子任务。

        参数:
            无。

        返回:
            ``True`` 表示存在父 task（即该任务为 delegation 子任务）。

        异常:
            无。

        副作用:
            无。
        """

        return self.parent_task_id is not None

    def to_dict(self) -> dict[str, str | int | None]:
        """将任务状态转换为可序列化为 JSON 的字典。

        参数:
            无。

        返回:
            包含任务字段的字典，``latest_turn_id`` / ``execution_status`` /
            ``parent_task_id`` / ``parent_turn_id`` / ``delegation_id`` /
            ``context_usage_used`` 可能为 None。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "task_id": self.task_id,
            "workspace_id": self.workspace_id,
            "agent_id": self.agent_id,
            "title": self.title,
            "status": self.status,
            "execution_status": self.execution_status,
            "task_type": self.task_type,
            "parent_task_id": self.parent_task_id,
            "parent_turn_id": self.parent_turn_id,
            "delegation_id": self.delegation_id,
            "context_usage_used": self.context_usage_used,
            "created_at": to_text(self.created_at),
            "updated_at": to_text(self.updated_at),
        }

    @classmethod
    def from_model(cls, row: TaskModel) -> "TaskRecord":
        """从 ORM 行构造任务记录值对象。

        参数:
            row: ``tasks`` 表的 SQLAlchemy 行对象。

        返回:
            对应的 ``TaskRecord``；文本时间戳经 ``from_text`` 还原为 datetime。

        异常:
            无。

        副作用:
            无。
        """
        return cls(
            task_id=row.task_id,
            workspace_id=row.workspace_id,
            agent_id=row.agent_id,
            title=row.title,
            status=row.status,
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
            task_type=row.task_type,
            parent_task_id=row.parent_task_id,
            parent_turn_id=row.parent_turn_id,
            delegation_id=row.delegation_id,
            context_usage_used=row.context_usage_used,
        )
