"""AgentTeamArgs：主 Agent 基于既有 Team 配置生成一次运行预览。"""

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    model_validator,
)

from app.agent_team.configuration.team_node_definition import TeamNodeIdentifier


class AgentTeamArgs(BaseModel):
    """主 Agent 基于既有 Team 配置生成一次运行预览。

    ``goal`` 与 ``node_goals`` 都属于本次运行输入，不会写入 Team 静态配置；确认后，
    总目标冻结在各节点 system prompt 中，节点子目标随 AgentTeamRun 冻结并作为对应节点的
    user input。
    """

    model_config = ConfigDict(extra="forbid")

    team_id: StrictStr = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        description=(
            "ID of an existing Agent Team configuration. This tool only runs an existing Team; "
            "it does not create or edit Team configurations."
        ),
    )
    goal: StrictStr = Field(
        min_length=1,
        max_length=50_000,
        description=(
            "Non-empty overall goal for this TeamRun. Every node should contribute toward it; "
            "the user can review and edit it before execution starts."
        ),
    )
    node_goals: dict[TeamNodeIdentifier, StrictStr] = Field(
        min_length=1,
        max_length=10,
        description=(
            "Provide exactly one non-empty subgoal for every node_id in the selected Team configuration. "
            "Do not omit nodes or add unknown node IDs. Give each node a specific responsibility and "
            "expected outcome, with boundaries that reduce overlap."
        ),
    )

    @model_validator(mode="after")
    def validate_run_inputs(self) -> "AgentTeamArgs":
        """在工具参数边界拒绝空白目标和保留节点标识。"""

        if not self.team_id.strip():
            raise ValueError("team_id must not be blank")
        if not self.goal.strip():
            raise ValueError("goal must not be blank")
        if any(not value.strip() for value in self.node_goals.values()):
            raise ValueError("node_goals values must not be blank")
        if "END" in self.node_goals:
            raise ValueError("END is a reserved terminal target, not a Team node")
        return self
