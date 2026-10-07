"""Agent Team 持久化运行状态的领域模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator


class AgentTeamNodeExecution(BaseModel):
    """记录一次节点 ConversationRun 执行及其提交结果。

    同一个节点可能因为转移回环被执行多次，因此该模型以 ``run_id`` 作为一次执行的
    唯一标识，不把节点标识当作执行记录的唯一键。节点尚未提交结果时，``completed``
    为假，``status`` 和 ``output`` 均为空。
    """

    model_config = ConfigDict(extra="forbid")

    node_id: StrictStr
    task_id: StrictInt
    run_id: StrictInt
    completed: bool = False
    status: StrictStr | None = None
    output: StrictStr | None = None


class AgentTeamTransitionRecord(BaseModel):
    """记录一次节点结果驱动的转移决策。"""

    model_config = ConfigDict(extra="forbid")

    from_node_id: StrictStr
    status: StrictStr
    target_node_id: StrictStr | None = None


class AgentTeamRunRuntime(BaseModel):
    """保存节点恢复执行所需的内部运行快照。"""

    model_config = ConfigDict(extra="forbid")

    node_snapshots: dict[str, dict[str, Any]] = Field(default_factory=dict)


class AgentTeamRunState(BaseModel):
    """Agent Team 的持久化执行快照。

    本模型保存 Team 的执行聚合状态，但不承载 Team 生命周期状态；生命周期状态由
    ``AgentTeamRunModel.status`` 及其条件状态迁移负责。``node_task_ids`` 按 ``node_id``
    固定节点 Task 身份；``node_executions`` 按创建顺序保存每次节点 Run，
    ``active_node_run_id`` 指向唯一尚未提交结果的节点 Run，节点结果和历史转移均从该快照
    读取。运行时快照只供后端恢复节点执行，API 投影时必须过滤。

    该模型使用严格字段和禁止额外字段的契约。状态结构发生变化时直接要求新数据库状态，
    不对旧 JSON 做兼容转换，避免同一进程同时维护多套状态格式。
    """

    model_config = ConfigDict(extra="forbid")

    node_executions: list[AgentTeamNodeExecution] = Field(default_factory=list)
    node_task_ids: dict[str, StrictInt] = Field(default_factory=dict)
    active_node_run_id: StrictInt | None = None
    transition_history: list[AgentTeamTransitionRecord] = Field(default_factory=list)
    runtime: AgentTeamRunRuntime = Field(default_factory=AgentTeamRunRuntime)

    @model_validator(mode="after")
    def validate_execution_cursor(self) -> AgentTeamRunState:
        """校验节点执行历史与活动节点游标的一致性。"""

        run_ids = [execution.run_id for execution in self.node_executions]
        if len(run_ids) != len(set(run_ids)):
            raise ValueError("Agent Team 节点执行记录不能复用同一个 run_id")
        active_executions = [
            execution for execution in self.node_executions if not execution.completed
        ]
        if len(active_executions) > 1:
            raise ValueError("Agent Team 状态最多只能存在一个活动节点")
        if active_executions:
            if self.active_node_run_id != active_executions[0].run_id:
                raise ValueError("活动节点游标与未完成节点执行记录不一致")
        elif self.active_node_run_id is not None:
            raise ValueError("没有活动节点执行记录时不能保留活动节点游标")
        unknown_node_ids = set(self.node_task_ids) - set(self.runtime.node_snapshots)
        if unknown_node_ids:
            raise ValueError("节点 Task 映射引用了不存在的 Team 节点")
        for execution in self.node_executions:
            if self.node_task_ids.get(execution.node_id) != execution.task_id:
                raise ValueError("节点执行记录与稳定 Task 映射不一致")
            if execution.completed and (execution.status is None or execution.output is None):
                raise ValueError("已完成节点必须同时保存 status 和 output")
            if not execution.completed and (
                execution.status is not None or execution.output is not None
            ):
                raise ValueError("未完成节点不能提前保存 status 或 output")
        return self

    @classmethod
    def initial(cls, node_runtime_snapshots: dict[str, dict[str, Any]]) -> AgentTeamRunState:
        """创建尚未启动节点的初始状态。

        参数:
            node_runtime_snapshots: 已解析并冻结的节点运行资源快照。
        """

        return cls(runtime=AgentTeamRunRuntime(node_snapshots=node_runtime_snapshots))

    def task_id_for_node(self, node_id: str) -> int | None:
        """返回节点已绑定的稳定 Task 标识；首次执行的节点返回 ``None``。"""

        return self.node_task_ids.get(node_id)

    def bind_node_task(self, node_id: str, task_id: int) -> None:
        """绑定节点的稳定 Task，拒绝把同一节点迁移到另一 Task。

        异常:
            ValueError: 节点未配置，或该节点已绑定不同的 Task。
        """

        if node_id not in self.runtime.node_snapshots:
            raise ValueError(f"Agent Team 状态没有节点运行快照: {node_id}")
        existing_task_id = self.node_task_ids.get(node_id)
        if existing_task_id is not None and existing_task_id != task_id:
            raise ValueError(f"Agent Team 节点不能更换 Task: {node_id}")
        self.node_task_ids[node_id] = task_id

    def start_node(self, node_id: str, task_id: int, run_id: int) -> None:
        """登记一次新节点执行，并将其设置为当前活动节点。

        异常:
            ValueError: 已存在未提交结果的活动节点，或输入标识不符合持久化契约。
        """

        if self.active_node_run_id is not None:
            raise ValueError("Agent Team 已存在尚未完成的活动节点")
        if self.node_task_ids.get(node_id) != task_id:
            raise ValueError("节点 Task 必须先绑定到 Agent Team 状态")
        execution = AgentTeamNodeExecution(node_id=node_id, task_id=task_id, run_id=run_id)
        self.node_executions.append(execution)
        self.active_node_run_id = run_id

    def complete_node(self, run_id: int, status: str, output: str) -> AgentTeamNodeExecution:
        """写入活动节点结果并清除活动节点游标。"""

        execution = self.execution_for_run(run_id)
        if execution is None:
            raise ValueError(f"找不到节点 Run: {run_id}")
        if execution.completed:
            raise ValueError(f"节点 Run 已提交结果: {run_id}")
        if self.active_node_run_id != run_id:
            raise ValueError(f"节点 Run 不是当前活动节点: {run_id}")
        execution.completed = True
        execution.status = status
        execution.output = output
        self.active_node_run_id = None
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

    def execution_for_run(self, run_id: int) -> AgentTeamNodeExecution | None:
        """按 ConversationRun 标识查找节点执行记录。"""

        return next(
            (execution for execution in self.node_executions if execution.run_id == run_id),
            None,
        )

    def active_execution(self) -> AgentTeamNodeExecution | None:
        """返回当前活动节点执行记录；没有活动节点时返回 ``None``。"""

        if self.active_node_run_id is None:
            return None
        execution = self.execution_for_run(self.active_node_run_id)
        if execution is None:
            raise ValueError("Agent Team 状态的活动节点游标没有对应执行记录")
        return execution

    def completed_run_ids(self) -> set[int]:
        """返回已经提交节点结果的 ConversationRun 标识集合。"""

        return {
            execution.run_id for execution in self.node_executions if execution.completed
        }

    def node_references(self) -> list[tuple[int, int]]:
        """返回所有节点 Task/Run 引用，供取消和诊断使用。"""

        return [(execution.task_id, execution.run_id) for execution in self.node_executions]

    def previous_outputs_for(self, node_ids: list[str]) -> list[dict[str, Any]]:
        """返回指定节点最近一次已完成执行的结果，供下游节点构造输入。"""

        latest: dict[str, AgentTeamNodeExecution] = {}
        for execution in self.node_executions:
            if execution.completed and execution.node_id in node_ids:
                latest[execution.node_id] = execution
        return [
            {
                "node_id": node_id,
                "status": latest[node_id].status,
                "output": latest[node_id].output,
            }
            for node_id in node_ids
            if node_id in latest
        ]

    def to_json(self) -> dict[str, Any]:
        """转换为可直接写入 SQLAlchemy JSON 列的字典。"""

        return self.model_dump(mode="json")
