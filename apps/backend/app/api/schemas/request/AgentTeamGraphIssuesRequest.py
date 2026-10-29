"""编辑器实时检查 Team 图结构的请求体。"""

from pydantic import BaseModel, ConfigDict


class AgentTeamGraphStatusDraft(BaseModel):
    """草稿中的业务状态；颜色等展示字段忽略。"""

    model_config = ConfigDict(extra="ignore")

    name: str


class AgentTeamGraphNodeDraft(BaseModel):
    """草稿中的节点；只取参与图校验的字段，名称、Agent 等字段忽略。"""

    model_config = ConfigDict(extra="ignore")

    node_id: str
    statuses: list[AgentTeamGraphStatusDraft]


class AgentTeamGraphTransitionDraft(BaseModel):
    """草稿中的状态转移。"""

    model_config = ConfigDict(extra="ignore")

    from_node_id: str
    status: str
    target_node_id: str


class AgentTeamGraphIssuesRequest(BaseModel):
    """编辑中的 Team 草稿。

    字段只做类型约束、不做格式校验：草稿允许空 ID、未填完的转移，格式问题留到保存时
    由 ``AgentTeamConfiguration`` 报告；这里只回答图结构有哪些问题。
    """

    model_config = ConfigDict(extra="ignore")

    start_node_id: str
    nodes: list[AgentTeamGraphNodeDraft]
    transitions: list[AgentTeamGraphTransitionDraft]
