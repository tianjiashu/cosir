"""保存 Team 配置文件所需的本地参数请求体。"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class SaveAgentTeamConfigurationRequest(BaseModel):
    """保存 Team 配置文件所需的本地参数。"""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["system", "workspace"]
    workspace_id: int | None = None
    configuration: dict[str, Any]
