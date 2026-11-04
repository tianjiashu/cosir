"""Agent Team 运行状态的组成部件（值对象）。

这些模型是 :class:`AgentTeamRunState` 的构成部分：``node_executions`` 按节点绑定稳定
Task 并记录其 ConversationRun；``transition_history`` 记录节点结果驱动的转移决策；
``node_runtime`` 保存单节点执行配置与展示快照。它们只在 ``AgentTeamRunState`` 内部被聚合，
不承载 Team 生命周期状态。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr


class AgentTeamNodeExecution(BaseModel):
    """绑定节点的稳定 Task，并按执行顺序记录该节点创建的 ConversationRun。"""

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
    """保存单个节点执行所需配置及其展示快照。

    该模型把原先松散存放在 ``node_snapshots`` 内层字典里的节点运行资源固化为显式字段，
    其中完整 ``node_goal`` 是节点输入，``node_goal_summary`` 只用于执行态展示。运行时资源
    只供后端恢复执行，API 投影时必须过滤。除 ``agent_id`` 和摘要外字段允许缺省，便于局部
    快照构造；``extra="forbid"`` 仍拦截未声明字段的漂移。
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: StrictStr
    role: StrictStr = ""
    node_goal: StrictStr = ""
    node_goal_summary: StrictStr = Field(min_length=1, max_length=10)
    system_prompt: StrictStr = ""
    allowed_tools: list[str] = Field(default_factory=list)
    structured_output: dict[str, Any] = Field(default_factory=dict)
    model_config_id: StrictInt | None = None
    model_settings: dict[str, Any] = Field(default_factory=dict)
