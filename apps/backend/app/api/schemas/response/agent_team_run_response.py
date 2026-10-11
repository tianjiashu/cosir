"""Agent Team 固定节点流水线的只读响应契约。"""

from pydantic import BaseModel, ConfigDict


class AgentTeamAgentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: int | None
    title: str
    agent_id: str | None
    role: str
    status: str
    is_primary: bool


class AgentTeamBusinessStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    color: str


class AgentTeamNodeVisitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visit_number: int
    conversation_run_id: int | None
    status: str
    business_status: str | None
    business_status_color: str | None
    failure_code: str | None
    failure_message: str | None
    agents: list[AgentTeamAgentResponse]


class AgentTeamNodeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    name: str
    agent_id: str
    role: str
    statuses: list[AgentTeamBusinessStatusResponse]
    goal: str
    goal_summary: str
    task_id: int | None
    status: str
    visits: list[AgentTeamNodeVisitResponse]


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
    current_node_id: str | None
    end_reason: str | None
    failure_node_id: str | None
    started_at: str | None
    ended_at: str | None
