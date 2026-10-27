"""描述一次 TeamRun 中单个节点的执行目标与展示摘要。"""

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator


# 模型可见提示：pydantic 会把类 docstring 渲染成 schema 的 ``description``，所以这里只留一句
# 英文契约；内部语义改用注释承载——摘要只服务于运行方案审阅与执行态展示，不参与节点提示词或
# 路由决策，只有 ``goal`` 会作为该节点的 user input。
class AgentTeamNodeGoal(BaseModel):
    """Subgoal of one Team node: the goal it executes, plus a short display label."""

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
