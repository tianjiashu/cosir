"""描述一次 TeamRun 中单个节点的执行目标与展示摘要。"""

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator


# 模型可见说明只在字段描述里（模型级 description 会被剥离）。摘要只服务于运行方案审阅与执行态
# 展示，不参与节点提示词或路由决策，只有 ``goal`` 会作为该节点的 user input。
class AgentTeamNodeGoal(BaseModel):
    """一个 Team 节点执行的目标与展示摘要。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    goal: StrictStr = Field(min_length=1, description="Full subgoal executed by this node.")
    summary: StrictStr = Field(
        min_length=1,
        max_length=10,
        description="Short display label for this node.",
    )

    @model_validator(mode="after")
    def validate_non_blank(self) -> "AgentTeamNodeGoal":
        """拒绝仅由空白组成的目标或摘要。"""

        if not self.goal.strip():
            raise ValueError("node goal must not be blank")
        if not self.summary.strip():
            raise ValueError("node goal summary must not be blank")
        return self
