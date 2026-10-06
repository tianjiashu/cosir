"""AgentTeamArgs：主 Agent 基于既有 Team 配置生成一次运行预览。"""

from pydantic import BaseModel, ConfigDict, Field, StrictStr


class AgentTeamArgs(BaseModel):
    """主 Agent 基于既有 Team 配置生成一次运行预览。

    ``instructions`` 与 ``goal`` 都属于本次运行输入，不会写入 Team 静态配置；运行
    确认后，它们会随 AgentTeamRun 一起冻结，供对应节点执行使用。
    """

    model_config = ConfigDict(extra="forbid")

    team_id: StrictStr = Field(min_length=1, max_length=128)
    goal: StrictStr = Field(min_length=1, max_length=50_000)
    instructions: dict[str, StrictStr] = Field(
        default_factory=dict,
        max_length=256,
        description="本次运行按 node_id 提供的节点预设指令，不属于 Team 静态配置。",
    )
