"""任务持久化状态值对象。

单一职责：承载一个任务的不可变业务数据并提供序列化（to_dict）。
不负责数据库操作（由 ``storage/crud/task_crud`` 负责）。

任务不再绑定 agent：agent 维度由 turn（用户任务首 turn）与 delegation 记录（子任务）
承载，``TaskRecord`` 仅描述 task 容器自身状态，不含 agent_id。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.storage.model.task_model import TaskModel
from app.utils.datetime_utils import from_text, to_text


@dataclass
class TaskRecord:
    """表示一个任务的持久化状态。"""

    id: int
    workspace_id: int
    title: str
    created_at: datetime
    updated_at: datetime
    extra: dict[str, Any] | None = None
    execution_status: str | None = None
    task_type: str = "user"
    parent_task_id: int | None = None
    parent_run_id: int | None = None
    delegation_id: int | None = None
    context_usage_used: int | None = None
    current_run_id: int | None = None
    context_window_total: int | None = None

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

    def to_dict(self) -> dict[str, object]:
        """将任务状态转换为可序列化为 JSON 的字典。

        参数:
            无。

        返回:
            包含任务字段的字典；``current_run_id``、``context_window_total``、
            ``execution_status``、``parent_task_id``、``parent_run_id``、``delegation_id``
            与 ``context_usage_used`` 可能为 None。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "title": self.title,
            "extra": self.extra,
            "task_type": self.task_type,
            "parent_task_id": self.parent_task_id,
            "parent_run_id": self.parent_run_id,
            "delegation_id": self.delegation_id,
            "context_usage_used": self.context_usage_used,
            "current_run_id": self.current_run_id,
            "context_window_total": self.context_window_total,
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
            id=row.id,
            workspace_id=row.workspace_id,
            title=row.title,
            extra=row.extra,
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
            task_type=row.task_type,
            parent_task_id=row.parent_task_id,
            parent_run_id=row.parent_run_id,
            delegation_id=None,
            context_usage_used=row.context_usage_used,
            current_run_id=row.current_run_id,
            context_window_total=row.context_window_total,
        )

    def to_model(self) -> TaskModel:
        """将 task 记录转换为 ORM 行。"""

        model_kwargs: dict[str, object] = {
            "workspace_id": self.workspace_id,
            "title": self.title,
            "extra": self.extra,
            "task_type": self.task_type,
            "parent_task_id": self.parent_task_id,
            "parent_run_id": self.parent_run_id,
            "current_run_id": self.current_run_id,
            "context_usage_used": self.context_usage_used,
            "context_window_total": self.context_window_total,
            "created_at": to_text(self.created_at),
            "updated_at": to_text(self.updated_at),
        }
        if self.id:
            model_kwargs["id"] = self.id
        return TaskModel(**model_kwargs)
