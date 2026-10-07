"""驳回待确认 Agent Team 并要求主 Agent 按反馈重新生成的请求体。"""

from pydantic import BaseModel, ConfigDict, Field


class RejectAgentTeamRequest(BaseModel):
    """提交非空审查意见，驳回对应的待确认 TeamRun。"""

    model_config = ConfigDict(extra="forbid")

    feedback: str = Field(min_length=1, max_length=4000)
