"""AgentTeamArgs：主 Agent 基于既有 Team 配置生成一次运行预览。"""

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    model_validator,
)

from app.agent_team.configuration.team_node_definition import TeamNodeIdentifier
from app.agent_team.node_goal import AgentTeamNodeGoal


# 模型可见提示：pydantic 会把类 docstring 渲染成 schema 的 ``description``，所以这里只留一句
# 英文契约；内部语义改用注释承载——``goal`` 与 ``node_goals`` 都是本次运行输入，不写入 Team
# 静态配置；完整子目标随 AgentTeamRun 冻结并作为对应节点的 user input，摘要只供审阅与展示。
class AgentTeamArgs(BaseModel):
    """Arguments of one Agent Team run: the Team, the overall goal, one subgoal per node."""

    model_config = ConfigDict(extra="forbid")

    team_id: StrictStr = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        description=(
            "team_id of the Team to run, exactly as listed in the available Agent Teams catalog."
        ),
    )
    goal: StrictStr = Field(
        min_length=1,
        max_length=50_00,
        description=(
            "Overall goal of this team: every node works toward it. Node agents start with a "
            "clean context, so state the task background in the goal when necessary."
        ),
    )
    node_goals: dict[TeamNodeIdentifier, AgentTeamNodeGoal] = Field(
        min_length=1,
        max_length=10,
        description=(
            "One entry per node of the selected Team: exactly its node ids, none omitted and no "
            "extras. Write each 'goal' so that node can carry it out without reading the other "
            "nodes' work, and state the expected outcome."
        ),
    )

    @model_validator(mode="after")
    def validate_run_inputs(self) -> "AgentTeamArgs":
        """在工具参数边界拒绝空白目标和保留节点标识。"""

        if not self.team_id.strip():
            raise ValueError("team_id must not be blank")
        if not self.goal.strip():
            raise ValueError("goal must not be blank")
        if "END" in self.node_goals:
            raise ValueError("END is a reserved terminal target, not a Team node")
        return self
