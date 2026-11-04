"""Agent Team 持久化运行状态的领域模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.models.agent_team.configuration.agent_team_configuration import (
    AgentTeamConfiguration,
)
from app.models.agent_team.state.agent_team_run_components import (
    AgentTeamNodeExecution,
    AgentTeamRunRuntime,
    AgentTeamTransitionRecord,
)
from app.models.enums.agent_team_run_end_reason import AgentTeamRunEndReason
from app.models.enums.agent_team_run_status import AgentTeamRunStatus

# 归因到某个节点的失败原因：Coordinator 落定这些失败时游标停在出事的节点上。
_NODE_FAILURE_REASONS = frozenset({
    AgentTeamRunEndReason.NODE_RUN_FAILED.value,
    AgentTeamRunEndReason.NODE_RUN_CANCELLED.value,
    AgentTeamRunEndReason.NODE_OUTPUT_INVALID.value,
    AgentTeamRunEndReason.TRANSITION_NOT_FOUND.value,
    AgentTeamRunEndReason.NEXT_NODE_START_FAILED.value,
})


class AgentTeamRunState(BaseModel):
    """Agent Team 的持久化执行快照。

    本模型保存 Team 的执行聚合状态，但不承载 Team 生命周期状态；生命周期状态由
    ``AgentTeamRunModel.status`` 及其条件状态迁移负责。``node_executions`` 按节点固定 Task
    身份并保存该节点创建的 ConversationRun ID；``current_node_id`` 是协调器的节点游标。
    ``node_runtime`` 保存节点执行配置和短摘要；执行配置只供后端恢复执行，API 投影时必须过滤。

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
    def initial(
        cls,
        node_runtime_snapshots: dict[str, dict[str, Any]],
        configuration: AgentTeamConfiguration,
    ) -> AgentTeamRunState:
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

    def has_next_node(self, status: str) -> str | None:
        """按当前节点与业务状态查找转移；缺少边由 Coordinator 收敛为可诊断失败。"""

        return self.node_transitions.get(self.current_node_id, {}).get(status)

    def node_execution_for_node(self, node_id: str) -> AgentTeamNodeExecution | None:
        """返回节点已绑定的稳定 Task 标识；首次执行的节点返回 ``None``。"""

        return self.node_executions.get(node_id)

    def create_execution_node(
        self, node_id: str, execution: AgentTeamNodeExecution
    ) -> AgentTeamNodeExecution:
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

    def failed_node_id(self, team_status: str, end_reason: str | None) -> str | None:
        """Team 因某个节点失败而落定时返回该节点；其余失败（超轮数、启动失败等）与非失败为 None。

        运行档案与前端读模型共用这一条归因规则。
        """

        if team_status == AgentTeamRunStatus.FAILED.value and end_reason in _NODE_FAILURE_REASONS:
            return self.current_node_id
        return None

    def to_json(self) -> dict[str, Any]:
        """转换为可直接写入 SQLAlchemy JSON 列的字典。"""

        return self.model_dump(mode="json")
