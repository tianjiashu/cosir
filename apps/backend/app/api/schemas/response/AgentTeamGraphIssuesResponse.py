"""Team 图结构问题列表响应。"""

from pydantic import BaseModel

from app.models.agent_team.configuration.graph_validation import TeamGraphIssueCode


class AgentTeamGraphIssueResponse(BaseModel):
    """一条图结构问题；定位字段供前端标到节点、状态出口或连线上。"""

    code: TeamGraphIssueCode
    message: str
    node_id: str | None
    status: str | None
    transition_index: int | None


class AgentTeamGraphIssuesResponse(BaseModel):
    """图结构问题列表；为空表示结构完整。"""

    issues: list[AgentTeamGraphIssueResponse]
