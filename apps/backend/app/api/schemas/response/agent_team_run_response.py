"""Agent Team 固定节点流水线的只读响应契约。"""

from pydantic import BaseModel, ConfigDict


class AgentTeamAgentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: int | None
    title: str
    agent_id: str | None
    role: str
    status: str


class AgentTeamNodeVisitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visit_number: int
    conversation_run_id: int | None
    status: str
    agents: list[AgentTeamAgentResponse]


class AgentTeamNodeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    name: str
    agent_id: str
    role: str
    goal_summary: str
    status: str
    visits: list[AgentTeamNodeVisitResponse]


class AgentTeamRunResponse(BaseModel):
    """供执行卡渲染的 TeamRun 投影，不包含可恢复执行用的内部 state。"""

    model_config = ConfigDict(extra="forbid")

    id: int
    team_id: str
    goal: str
    status: str
    nodes: list[AgentTeamNodeResponse]
    current_node_id: str | None
