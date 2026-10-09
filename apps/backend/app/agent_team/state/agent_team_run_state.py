"""Agent Team 持久化运行状态的领域模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from app.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry


class AgentTeamNodeExecution(BaseModel):
    """记录一次节点 ConversationRun 执行及其提交结果。

    同一个节点可能因为转移回环被执行多次，因此该模型以 ``run_id`` 作为一次执行的
    唯一标识，不把节点标识当作执行记录的唯一键。节点尚未提交结果时，``completed``
    为假，``status`` 和 ``output`` 均为空。
    """

    model_config = ConfigDict(extra="forbid")

    node_id: StrictStr
    task_id: StrictInt
    conversation_run_ids: list[int] = Field(default_factory=list)
    status: StrictStr | None = None


class AgentTeamTransitionRecord(BaseModel):
    """记录一次节点结果驱动的转移决策。"""

    model_config = ConfigDict(extra="forbid")

    from_node_id: StrictStr
    status: StrictStr
    target_node_id: StrictStr | None = None


class AgentTeamRunRuntime(BaseModel):
    """保存单个节点恢复执行所需的运行快照。

    该模型把原先松散存放在 ``node_snapshots`` 内层字典里的节点运行资源固化为显式字段，
    只供后端恢复节点执行，API 投影时必须过滤。除 ``agent_id`` 外字段允许缺省，便于局部
    快照构造；``extra="forbid"`` 仍拦截未声明字段的漂移。
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: StrictStr
    role: StrictStr = ""
    node_goal: StrictStr = ""
    system_prompt: StrictStr = ""
    allowed_tools: list[str] = Field(default_factory=list)
    structured_output: dict[str, Any] = Field(default_factory=dict)
    model_config_id: StrictInt | None = None
    model_settings: dict[str, Any] = Field(default_factory=dict)


class AgentTeamRunState(BaseModel):
    """Agent Team 的持久化执行快照。

    本模型保存 Team 的执行聚合状态，但不承载 Team 生命周期状态；生命周期状态由
    ``AgentTeamRunModel.status`` 及其条件状态迁移负责。``node_task_ids`` 按 ``node_id``
    固定节点 Task 身份；``node_executions`` 按创建顺序保存每次节点 Run，
    ``active_node_run_id`` 指向唯一尚未提交结果的节点 Run，节点结果和历史转移均从该快照
    读取。``node_runtime`` 按节点保存运行配置和该节点的 ``node_goal``，是节点
    执行计划的唯一持久化来源。运行时快照只供后端恢复节点执行，API 投影时必须过滤。

    该模型使用严格字段和禁止额外字段的契约。状态结构发生变化时直接要求新数据库状态，
    不对旧 JSON 做兼容转换，避免同一进程同时维护多套状态格式。
    """

    model_config = ConfigDict(extra="forbid")

    node_executions: dict[str, AgentTeamNodeExecution] = Field(default_factory=dict)
    current_node_id: StrictStr | None = None
    transition_history: list[AgentTeamTransitionRecord] = Field(default_factory=list)
    node_runtime: dict[str, AgentTeamRunRuntime] = Field(default_factory=dict)
    node_transitions: dict[str, dict[str, str]] = Field(default_factory=dict)

    @classmethod
    def initial(cls, node_runtime_snapshots: dict[str, dict[str, Any]],
                configuration: AgentTeamConfiguration) -> AgentTeamRunState:
        """创建尚未启动节点的初始状态。

        参数:
            node_runtime_snapshots: 已解析并冻结的节点运行资源快照，按 ``node_id`` 索引；
                每个快照都会被校验并固化为 ``AgentTeamRunRuntime``。
            configuration: 已冻结的 Team 配置；其转移规则被压平为
                ``node_transitions[node_id][status] = target_node_id`` 快照。
        """

        node_transitions: dict[str, dict[str, str]] = {}
        for transition in configuration.transitions:
            node_transitions.setdefault(transition.from_node_id, {})[
                transition.status
            ] = transition.target_node_id

        return cls(
            node_runtime={
                node_id: AgentTeamRunRuntime.model_validate(snapshot)
                for node_id, snapshot in node_runtime_snapshots.items()
            },
            node_transitions=node_transitions,
        )

    def has_next_node(self,status: str) -> str:
        """返回是否有下一个节点需要执行。"""

        return self.node_transitions[self.current_node_id].get(status)

    def node_execution_for_node(self, node_id: str) -> AgentTeamNodeExecution | None:
        """返回节点已绑定的稳定 Task 标识；首次执行的节点返回 ``None``。"""

        return self.node_executions.get(node_id)

    def create_execution_node(self, node_id: str, execution: AgentTeamNodeExecution) -> AgentTeamNodeExecution:
        """创建节点执行记录。"""

        self.node_executions[node_id] = execution
        return execution

    def add_transition(
            self,
            from_node_id: str,
            status: str,
            target_node_id: str | None,
    ) -> None:
        """追加一次节点转移记录。"""

        self.transition_history.append(
            AgentTeamTransitionRecord(
                from_node_id=from_node_id,
                status=status,
                target_node_id=target_node_id,
            )
        )

    def cancel(self):
        node_id = self.current_node_id
        if node_id is None:
            return
        execution = self.node_executions[node_id]
        if not execution.conversation_run_ids:
            return
        run_id = execution.conversation_run_ids[-1]
        if cancellation_registry.is_cancelled(run_id):
            return
        cancellation_registry.mark_cancelled(run_id)

    def node_references(self) -> list[tuple[int, int]]:
        """返回所有节点 Task/Run 引用，供取消和诊断使用。"""

        return [(execution.task_id, execution.run_id) for execution in self.node_executions]

    def to_json(self) -> dict[str, Any]:
        """转换为可直接写入 SQLAlchemy JSON 列的字典。"""

        return self.model_dump(mode="json")
