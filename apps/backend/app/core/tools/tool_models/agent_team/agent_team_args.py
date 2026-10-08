"""AgentTeamArgs：主 Agent 基于既有 Team 配置生成一次运行预览。"""

from pydantic import BaseModel, ConfigDict, Field, StrictStr


class AgentTeamArgs(BaseModel):
    """主 Agent 基于既有 Team 配置生成一次运行预览。

    ``goal`` 与 ``node_goals`` 都属于本次运行输入，不会写入 Team 静态配置；确认后，
    总目标冻结在各节点 system prompt 中，节点子目标随 AgentTeamRun 冻结并作为对应节点的
    user input。
    """

    model_config = ConfigDict(extra="forbid")

    team_id: StrictStr = Field(min_length=1, max_length=128,
                               description="ID of an existing Team configuration; must reference a valid Team config, otherwise an error is raised.")
    goal: StrictStr = Field(
        min_length=1,
        max_length=50_000,
        description="Required overall goal for the Agent Team; all nodes should contribute toward achieving it.",
    )
    node_goals: dict[str, StrictStr] = Field(
        min_length=1,
        max_length=256,
        description=(
            "Required mapping keyed by every node_id in the Team configuration. Give each node a specific, "
            "non-empty responsibility and expected outcome. Split the overall goal into complementary "
            "assignments, adding necessary boundaries or constraints to avoid overlap."
        ),
    )
