"""Task 的持久化附属数据：分叉来源或 Team 节点配置，不承载执行生命周期。"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.conversation_run_options import ConversationRunOptions
from app.models.task_kind import FORK_TASK_TYPES, TaskType


class TaskForkLaunch(BaseModel):
    """分支的初始配置；已删除的 Agent/模型身份可为空，用户重新选择后仍可继续。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str | None
    model_config_id: int | None
    options: ConversationRunOptions


class TaskFork(BaseModel):
    """不可变分叉来源与可改挂的侧栏关系；创建方式只由 TaskType 表达。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_task_id: int
    source_title: str
    tree_parent_task_id: int | None
    initial_launch: TaskForkLaunch


class TaskNodeConfig(BaseModel):
    """节点 Task 执行时需要的冻结配置；身份、目标和工具分别归 Run、输入和 Task schema。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str
    system_prompt: str
    structured_output: dict[str, Any]
    model_settings: dict[str, Any]


class TaskExtra(BaseModel):
    """现有 Task JSON 列的严格结构。普通与委派 Task 使用空对象，不开放任意扩展键。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fork: TaskFork | None = None
    team_node: TaskNodeConfig | None = None

    def validate_task_type(self, task_type: TaskType) -> None:
        """在持久化读取边界一次校验类型与附属数据，错误数据直接失败。"""
        if (self.fork is not None) != (task_type in FORK_TASK_TYPES):
            raise ValueError(f"{task_type} carries invalid fork metadata")
        if (self.team_node is not None) != (task_type == TaskType.TEAM_NODE_TASK):
            raise ValueError(f"{task_type} carries invalid team node metadata")
