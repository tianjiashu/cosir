"""Agent Team 执行卡的安全只读响应契约。"""

from pydantic import BaseModel, ConfigDict


class AgentTeamChildAgentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: int
    title: str
    status: str


class AgentTeamNodeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    name: str
    agent_id: str
    goal: str
    task_id: int | None
    status: str
    conversation_run_count: int
    agent_count: int
    child_agents: list[AgentTeamChildAgentResponse]


class AgentTeamTransitionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_node_id: str
    status: str
    target_node_id: str


class AgentTeamTransitionHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_node_id: str
    status: str
    target_node_id: str | None


class AgentTeamRunResponse(BaseModel):
    """供执行卡渲染的 TeamRun 投影，不包含可恢复执行用的内部 state。"""

    model_config = ConfigDict(extra="forbid")

    id: int
    team_id: str
    workspace_id: int
    parent_task_id: int
    parent_run_id: int
    goal: str
    status: str
    start_node_id: str
    nodes: list[AgentTeamNodeResponse]
    transitions: list[AgentTeamTransitionResponse]
    transition_history: list[AgentTeamTransitionHistoryResponse]
    current_node_id: str | None
    end_reason: str | None
    started_at: str | None
    ended_at: str | None
