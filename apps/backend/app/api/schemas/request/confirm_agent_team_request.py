"""确认 Agent Team 运行意图的请求体。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr


class ConfirmAgentTeamRequest(BaseModel):
    """提交用户最终编辑后的配置并确认一次 Agent Team 执行。

    TeamRun 通过主 Task、主 Run 和 Team 标识定位；配置始终由后端重新校验和解析，
    不信任前端展示数据中的运行快照或指纹。
    """

    model_config = ConfigDict(extra="forbid")

    parent_task_id: int = Field(gt=0)
    parent_run_id: int = Field(gt=0)
    team_id: StrictStr = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    configuration: dict[str, Any]
