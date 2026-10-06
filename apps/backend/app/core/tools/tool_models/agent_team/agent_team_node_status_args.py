"""AgentTeamNodeStatusArgs：子 Agent 提交当前节点业务状态和任意输出。"""

from pydantic import BaseModel, ConfigDict, Field, StrictStr


class AgentTeamNodeStatusArgs(BaseModel):
    """子 Agent 提交当前节点业务状态和任意输出。"""

    model_config = ConfigDict(extra="forbid")
    status: StrictStr = Field(min_length=1, max_length=128)
    output: StrictStr = Field(max_length=100_000)
