"""AgentTeamArgs：主 Agent 基于既有 Team 配置生成一次运行预览。"""

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    model_validator,
)

from app.models.agent_team.configuration.team_node_definition import TeamNodeIdentifier
from app.core.tool.agent_team.node_goal import AgentTeamNodeGoal


class AgentTeamNodeGoalArgs(AgentTeamNodeGoal):
    """一个 Team 节点的子目标与审阅摘要。"""

    # 模型侧用显式标识替代动态字典键；目标与摘要沿用领域约束。
    node_id: TeamNodeIdentifier = Field(description="ID of the node receiving this subgoal.")


class AgentTeamArgs(BaseModel):
    """一次 Team 运行的输入：既有配置、总目标与每个节点的子目标。"""

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
    node_goals: list[AgentTeamNodeGoalArgs] = Field(
        min_length=1,
        max_length=10,
        description=(
            "One object per node of the selected Team, with node_id, goal and summary: "
            "exactly its node ids, none omitted, no duplicates and no "
            "extras. Write each 'goal' so that node can carry it out without reading the other "
            "nodes' work, and state the expected outcome."
        ),
    )

    @model_validator(mode="after")
    def validate_run_inputs(self) -> "AgentTeamArgs":
        """在工具参数边界拒绝空白目标、保留标识和会导致映射覆盖的重复节点。"""

        if not self.team_id.strip():
            raise ValueError("team_id must not be blank")
        if not self.goal.strip():
            raise ValueError("goal must not be blank")
        node_ids = [item.node_id for item in self.node_goals]
        if "END" in node_ids:
            raise ValueError("END is a reserved terminal target, not a Team node")
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("node_goals must contain each node_id only once")
        return self
