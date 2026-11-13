"""任务持久化状态值对象。

单一职责：承载一个任务的不可变业务数据并提供序列化（to_dict）。
不负责数据库操作（由 ``storage/crud/task_crud`` 负责）。

Task 不绑定 Agent：Agent 身份随入队输入走、在建 Run 时固化到 Run，``TaskRecord`` 只描述
Task 容器自身状态。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from app.models.task_extra import TaskExtra, TaskFork
from app.models.task_kind import TaskType
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
    extra: TaskExtra = field(default_factory=TaskExtra)
    tool_schemas: list[dict[str, Any]] = field(default_factory=list)
    task_type: TaskType = TaskType.USER
    parent_task_id: int | None = None
    current_run_id: int | None = None
    creation_command_id: str | None = None
    # 派生深度（见 ``models.task_kind``）；创建后不变，所以缓存在运行时空间里的记录也可信。
    derivation_depth: int = 0

    def deriver_id(self) -> int | None:
        """派生出本 Task 的那个 Task（委派或 Agent fork 的发起方）；不是 Agent 派生的为 ``None``。

        ``send_message`` / ``task_status`` 的寻址只认这条父子关系。不能只看 ``parent_task_id``：
        Agent fork 不挂所有权子树，发起方记在 ``extra.fork``；用户手动 fork 的不算。
        """
        if self.task_type == TaskType.AGENT_TASK:
            return self.parent_task_id
        if self.task_type == TaskType.AGENT_FORK:
            return cast(TaskFork, self.extra.fork).source_task_id
        return None

    def derived_by(self, task_id: int) -> bool:
        """本 Task 是否由 ``task_id`` 的 Agent 派生。"""
        return self.deriver_id() == task_id

    def to_dict(self) -> dict[str, object]:
        """将任务状态转换为可序列化为 JSON 的字典。

        参数:
            无。

        返回:
            包含任务字段的字典；``current_run_id`` 与
            ``parent_task_id`` 可能为 None。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "title": self.title,
            "extra": self.extra.model_dump(mode="json"),
            "task_type": self.task_type,
            "parent_task_id": self.parent_task_id,
            "current_run_id": self.current_run_id,
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
        task_type = TaskType(row.task_type)
        extra = TaskExtra.model_validate(row.extra or {})
        extra.validate_task_type(task_type)
        return cls(
            id=row.id,
            workspace_id=row.workspace_id,
            title=row.title,
            extra=extra,
            tool_schemas=row.tool_schemas or [],
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
            task_type=task_type,
            parent_task_id=row.parent_task_id,
            current_run_id=row.current_run_id,
            creation_command_id=row.creation_command_id,
            derivation_depth=row.derivation_depth,
        )

    def to_model(self) -> TaskModel:
        """将 task 记录转换为 ORM 行。"""

        model_kwargs: dict[str, object] = {
            "workspace_id": self.workspace_id,
            "title": self.title,
            "extra": self.extra.model_dump(mode="json"),
            "tool_schemas": self.tool_schemas or [],
            "task_type": self.task_type,
            "parent_task_id": self.parent_task_id,
            "current_run_id": self.current_run_id,
            "creation_command_id": self.creation_command_id,
            "derivation_depth": self.derivation_depth,
            "created_at": to_text(self.created_at),
            "updated_at": to_text(self.updated_at),
        }
        if self.id:
            model_kwargs["id"] = self.id
        return TaskModel(**model_kwargs)
