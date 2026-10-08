"""提交待确认 Agent Team 的用户审阅输入与必填驳回意见。"""

from pydantic import BaseModel, ConfigDict, Field, StrictStr


class RejectAgentTeamRequest(BaseModel):
    """提交用户审阅后的运行输入和必填驳回意见。"""

    model_config = ConfigDict(extra="forbid")

    goal: StrictStr = Field(
        min_length=1,
        max_length=50_000,
        description="用户在预览中审阅后的 Agent Team 总目标。",
    )
    node_goals: dict[StrictStr, StrictStr] = Field(
        min_length=1,
        max_length=256,
        description="用户在预览中审阅后的完整节点子目标映射，须覆盖 Team 的全部节点。",
    )
    feedback: StrictStr = Field(
        min_length=1,
        max_length=4000,
        description="必填驳回意见，供主 Agent 修订并重新提交方案。",
    )
