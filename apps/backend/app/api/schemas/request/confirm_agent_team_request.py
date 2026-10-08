"""确认 Agent Team 运行意图的请求体。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConfirmAgentTeamRequest(BaseModel):
    """提交用户最终编辑后的配置并确认一次 Agent Team 执行。

    TeamRun 通过工具预览中对应的 ``team_run_id`` 精确定位；配置始终由后端重新校验和
    解析，不信任前端展示数据中的运行快照或指纹。
    """

    model_config = ConfigDict(extra="forbid")

    team_run_id: int = Field(gt=0)
    configuration: dict[str, Any]
    goal: str | None = None
    node_goals: dict[str, str] | None = Field(
        default=None,
        description=(
            "可选的完整节点子目标映射，键必须覆盖 Team 配置中的每个 node_id，值作为该节点的 user input；"
            "未提供时沿用待确认方案中已校验的子目标。"
        ),
    )
