"""Task 的持久化附属数据：分叉来源或 Team 节点配置，不承载执行生命周期。"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.conversation_run_options import ConversationRunOptions
from app.models.conversation_run_record import ConversationRunRecord
from app.models.task_kind import FORK_TASK_TYPES, TaskType

# Agent 派生出的 Task：记下派生它的那次工具调用（撤回那一回合时据此收回）。
_AGENT_DERIVED_TYPES = frozenset({TaskType.AGENT_TASK, TaskType.AGENT_FORK})


class TaskForkLaunch(BaseModel):
    """分支的初始配置；已删除的 Agent/模型身份可为空，用户重新选择后仍可继续。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str | None
    model_config_id: str | None
    options: ConversationRunOptions

    @classmethod
    def from_run(cls, run: ConversationRunRecord) -> "TaskForkLaunch":
        """冻结某个 Run 的发起配置作为分支的初始配置。"""

        return cls(agent_id=run.agent_id, model_config_id=run.model_config_id, options=run.options)


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
    # 派生出本 Task 的那次调用（委派或 Agent fork 在发起方 Task 里的 ``tool_call_id``）；发起方是
    # ``TaskRecord.deriver_id()``。撤回发起方的一个回合时，按它找出这一回合派生的 Task 一并收回。
    deriving_call_id: str | None = None

    def validate_task_type(self, task_type: TaskType) -> None:
        """在持久化读取边界一次校验类型与附属数据，错误数据直接失败。"""
        if (self.fork is not None) != (task_type in FORK_TASK_TYPES):
            raise ValueError(f"{task_type} carries invalid fork metadata")
        if (self.team_node is not None) != (task_type == TaskType.TEAM_NODE_TASK):
            raise ValueError(f"{task_type} carries invalid team node metadata")
        # 只有 Agent 派生出的 Task 能带；委派与 Agent fork 两个入口总会写上。
        if self.deriving_call_id is not None and task_type not in _AGENT_DERIVED_TYPES:
            raise ValueError(f"{task_type} carries invalid deriving call metadata")
